# Palinode 🧠

**Persistent long-term memory for AI agents — with provenance.**

*A palinode is a poem that retracts what was said before and says it better.
That's what memory compaction does.*

Git-native. Markdown-first. No database required.

---

## The Problem

AI agents wake up with amnesia every session. They don't remember who you are, what you're working on, or what was decided yesterday. Current solutions either don't scale (flat files), produce uncurated noise (vector-only stores), or lock you into opaque databases you can't inspect.

## The Solution

Palinode is persistent memory for LLM agents that stores everything as **typed markdown files** — human-readable, git-versioned, greppable. A hybrid search index (SQLite-vec + BM25) makes memories searchable by meaning *and* keyword. An MCP server exposes 13 tools so Claude Code, OpenClaw, Hermes, or any MCP client can search, save, and manage memories. An OpenClaw plugin handles automatic context injection and capture.

Works with any LLM backend. Tested with OLMo, Qwen, and Claude. If every service crashes, `cat` still works.

---

## What Makes Palinode Different

Most agent memory systems are opaque databases you can't inspect, flat files that don't scale, or graph stores that require infrastructure. Palinode is **memory with provenance** — the only system where you can `git blame` every fact your agent knows.

### No other production system has these:

- **Git blame/diff/rollback as agent tools** — not just git-compatible files, but `palinode_diff`, `palinode_blame`, and `palinode_rollback` as first-class MCP tools your agent can call. [DiffMem](https://github.com/search?q=diffmem) and Git-Context-Controller are PoCs; Palinode ships 13 MCP tools including 5 git operations.

- **Operation-based compaction with a deterministic executor** — the LLM outputs structured ops (KEEP/UPDATE/MERGE/SUPERSEDE/ARCHIVE), a deterministic executor applies them. The LLM never touches your files directly. [All-Mem](https://arxiv.org/search/?query=all-mem+memory) does something similar on graph nodes; Palinode does it on plain markdown with git commits.

- **Per-fact addressability** — every list item gets an invisible `<!-- fact:slug -->` ID that survives git operations and is targetable by compaction. memsearch has per-chunk (heading-level); Hermes has per-entry (delimiter). Nobody has inline fact IDs.

- **4-phase injection pipeline** — Core → Topic → Associative → Triggered. Individual phases exist elsewhere (Letta core, LangMem search, Zep graph, ADK preload), but no system combines all four. [Perplexity deep research confirms](docs/perplexity-landscape-2026-03-31.md): "No widely documented system matches a four-phase pipeline with exactly the requested semantics."

- **If every service crashes, `cat` still works** — your memory is markdown files in a directory. Rebuild the index from files anytime.

---

## Features

> ✅ = production-ready &nbsp; 🧪 = implemented, beta &nbsp;

### Memory Storage ✅
- **Typed memories** — people, projects, decisions, insights, research (not flat text blobs)
- **Layered structure** — files split into Identity (`name.md`), Status (`-status.md`), and History (`-history.md`)
- **Fact IDs** — persistent, unique IDs (`<!-- fact:slug -->`) for precise auditing and compaction
- **YAML frontmatter** — structured metadata, categories, entity cross-references
- **Git-versioned** — every memory change has a commit, `git blame` your agent's brain
- **Graceful degradation** — vector index down → files still readable, grep still works

### Capture ✅
- **Session-end extraction** — auto-captures key facts from conversations to daily notes
- **`-es` quick capture** — append `-es` to any message to route it into the right memory bucket
- **Inbox pipeline** — drop PDFs, audio, URLs into a watch folder; they appear as research references

### Recall
- ✅ **Core memory injection** — files marked `core: true` are always in context
- ✅ **Tiered injection** — full content on turn 1, summaries on subsequent turns (saves tokens)
- ✅ **Hybrid search** — BM25 keyword matching + vector similarity merged with Reciprocal Rank Fusion
- ✅ **Content-hash dedup** — SHA-256 hashing skips re-embedding unchanged files (~90% savings)
- 🧪 **Temporal decay** — re-ranks results based on freshness and importance (beta — decay constants need tuning)
- 🧪 **Associative recall** — spreading activation across entity graph (beta)
- 🧪 **Prospective triggers** — auto-inject files when trigger contexts match (beta)

### Compaction 🧪
- **Operation-based** — LLM outputs JSON ops, deterministic executor applies them
- **Layered files** — Identity (slow-changing) / Status (fast-changing) / History (archived)
- **Weekly consolidation** — cron-driven, local LLM (OLMo/vLLM), git commits each pass
- **Security scanning** — blocks prompt injection and credential exfiltration in memory writes

### Integration ✅
- **OpenClaw plugin** — lifecycle hooks for inject, extract, and capture
- **MCP server** — 13 tools for Claude Code and any MCP client
- **FastAPI server** — HTTP API for programmatic access
- **CLI** — command-line search, stats, reindex

---

## Architecture

```mermaid
graph TD
    subgraph Capture
        T[Telegram] --> OC[OpenClaw Agent]
        S[Slack] --> OC
        W[Webchat] --> OC
        CC[Claude Code] --> MCP[MCP Server]
    end

    subgraph Processing
        OC -->|session end| EX[Extract to daily notes]
        OC -->|"-es" flag| QC[Quick capture + route]
        MCP -->|palinode_save| SV[Write markdown file]
    end

    subgraph Storage
        EX --> MD[~/palinode/*.md]
        QC --> MD
        SV --> MD
        MD -->|file watcher| IDX{Index}
        IDX -->|embed| VEC[(SQLite-vec)]
        IDX -->|tokenize| FTS[(FTS5 BM25)]
    end

    subgraph Recall
        Q[Search query] --> HYB{Hybrid search}
        HYB --> VEC
        HYB --> FTS
        HYB -->|RRF merge| RES[Ranked results]
    end
```

### Stack

| Layer | Technology | Why |
|---|---|---|
| Source of truth | Markdown + YAML frontmatter | Human-readable, git-versioned, portable |
| Semantic index | SQLite-vec (embedded) | No server, single file, zero config |
| Keyword index | SQLite FTS5 (embedded) | BM25 for exact terms, stdlib — no dependencies |
| Embeddings | BGE-M3 via Ollama (local) | Private, no API dependency, 1024d |
| API | FastAPI on port 6340 | Lightweight HTTP interface |
| MCP | Python MCP SDK (stdio) | Claude Code + any MCP client |
| Plugin | OpenClaw Plugin SDK | Lifecycle hooks for session inject/extract |
| Behavior spec | `PROGRAM.md` | Change how the memory manager thinks by editing one file |

---

## Requirements

- **Python 3.12+**
- **Ollama** with `bge-m3` model (for embeddings — `ollama pull bge-m3`)
- **Git** (for memory versioning)
- A directory for your memory files (local, or a private git repo)

Optional:
- **OpenClaw** (for agent plugin integration)
- **vLLM or Ollama with a chat model** (for weekly consolidation — any 7B+ model works)

### Tested With

| Component | Version |
|---|---|
| Embeddings | BGE-M3 via Ollama |
| Consolidation LLM | [OLMo 3.1 32B AWQ](https://huggingface.co/allenai/OLMo-3.1-32B-AWQ) via vLLM |
| Hardware | RTX 5090 32GB (consolidation), any CPU (embeddings + API) |
| Python | 3.12 |
| OS | Ubuntu 22.04 (Linux), macOS 14+ (development) |

Other models should work — the consolidation prompt is model-agnostic. Smaller models (8B) may produce less reliable JSON for compaction operations; use `json-repair` (included) as a safety net.

## Quick Start

### 1. Clone and install

```bash
git clone https://github.com/Paul-Kyle/palinode
cd palinode
python3 -m venv venv && source venv/bin/activate
pip install -e .
```

### 2. Create your memory directory

Your memories live in a separate directory from the code — choose one:

**Option A: Local only (simplest)**
```bash
mkdir -p ~/.palinode/{people,projects,decisions,insights,daily}
cd ~/.palinode && git init
export PALINODE_DIR=~/.palinode
```

**Option B: Private GitHub repo (backup + multi-machine sync)**
```bash
# Create a PRIVATE repo on GitHub (e.g., yourname/palinode-data)
git clone https://github.com/yourname/palinode-data.git ~/.palinode
mkdir -p ~/.palinode/{people,projects,decisions,insights,daily}
export PALINODE_DIR=~/.palinode
```

**Option C: Self-hosted git server**
```bash
git clone git@your-server:palinode-data.git ~/.palinode
export PALINODE_DIR=~/.palinode
```

> **Important:** Your memory directory is PRIVATE. It contains personal data about you, your projects, and the people you work with. Never make it public. The code repo (`Paul-Kyle/palinode`) contains zero memory files — your data stays yours.

### 3. Configure

```bash
cp palinode.config.yaml.example ~/.palinode/palinode.config.yaml
```

Edit `~/.palinode/palinode.config.yaml`:
```yaml
memory_dir: "~/.palinode"          # Where your memory files live
ollama_url: "http://localhost:11434"  # Your Ollama instance
embedding_model: "bge-m3"            # Pull with: ollama pull bge-m3
```

### 4. Run services

```bash
# Start the API server
PALINODE_DIR=~/.palinode python -m palinode.api.server

# In another terminal: start the file watcher (auto-indexes on save)
PALINODE_DIR=~/.palinode python -m palinode.indexer.watcher

# Check health
curl http://localhost:6340/status
```

### 4. Use from Claude Code (MCP)

Add to `~/.claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "palinode": {
      "command": "ssh",
      "args": ["-o", "StrictHostKeyChecking=no",
               "user@your-server",
               "cd /path/to/palinode && venv/bin/python -m palinode.mcp"]
    }
  }
}
```

See [docs/claude-code-setup.md](docs/claude-code-setup.md) for details.

### 5. Use from OpenClaw

Install the plugin to your OpenClaw extensions directory:

```bash
cp -r plugin/ ~/.openclaw/extensions/openclaw-palinode
```

The plugin provides `before_agent_start` (inject), `agent_end` (extract), and `-es` capture hooks.

**Already using OpenClaw's built-in memory?** See [docs/INSTALL-OPENCLAW-MIGRATION.md](docs/INSTALL-OPENCLAW-MIGRATION.md) for what to disable (MEMORY.md, Mem0, session-memory hook) and why Palinode replaces all of them with ~5,700 fewer tokens per session.

---

## Git-Powered Memory

Palinode is the only memory system where you can `git blame` your agent's brain.

### What changed this week?
```bash
palinode diff --days 7
# or via tool: palinode_diff(days=7)
```

### When was this fact recorded?
```bash
palinode blame projects/my-app.md --search "Stripe"
# → 2026-03-10 a1b2c3d — Chose Stripe over Square for payment integration
```

### Show a file's evolution
```bash
palinode timeline projects/mm-kmd.md
# Shows every change with dates and descriptions
```

### Revert a bad consolidation
```bash
palinode rollback projects/mm-kmd.md --commit a1b2c3d
# Creates a new commit, nothing lost
```

### Sync to another machine
```bash
palinode push  # backup to GitHub
# On Mac: git pull in your palinode-data clone
```

### Browse your memory
```bash
# List all people
ls ~/.palinode/people/

# Read a person's memory file
cat ~/.palinode/people/alice.md

# Find all files about a topic
palinode_search("alice project decisions")

# See entity cross-references
palinode_entities("person/alice")
```

Memory files are plain markdown — edit with any text editor, VS Code, Obsidian, or `vim`. Changes are auto-indexed by the file watcher within seconds.

---

## Migrating from Mem0

If you have existing memories in Mem0 (Qdrant), Palinode can import them:

```bash
python -m palinode.migration.run_mem0_backfill
```

This exports all memories, deduplicates (~40-60% reduction), classifies
them by type (LLM-powered), groups related memories, and generates
typed markdown files. Review the output before reindexing.

---

## Memory File Format

```yaml
---
id: project-mm-kmd
category: project
name: my-app
core: true
status: active
entities: [person/paul, person/peter]
last_updated: 2026-03-29T00:00:00Z
summary: "Multi-agent murder mystery engine on LangGraph + OLMo 3.1."
---
# My App — Mobile Checkout Redesign

Your content here. Markdown, as detailed or brief as you want.
Palinode indexes it, searches it, and injects it when relevant.
```

Mark `core: true` for files that should always be in context. Everything else is retrieved on demand via hybrid search.

---

## Configuration

All behavior is configurable via `palinode.config.yaml`:

```yaml
recall:
  core:
    max_chars_per_file: 3000
  search:
    top_k: 5
    threshold: 0.4

search:
  hybrid_enabled: true     # BM25 + vector combined
  hybrid_weight: 0.5       # 0.0=vector only, 1.0=BM25 only

capture:
  extraction:
    max_items_per_session: 5
    types: [Decision, ProjectSnapshot, Insight, PersonMemory]

embeddings:
  primary:
    provider: ollama
    model: bge-m3
    url: http://localhost:11434
```

### Remote Model Endpoints (Mac Studio)
If you are running the `start_mlx_servers.sh` script on the Mac Studio, the following MLX models are exposed on the network:

See [palinode.config.yaml.example](palinode.config.yaml.example) for the complete reference with all defaults.

---

## Tools

Available in OpenClaw conversations and Claude Code (via MCP):

| Tool | Description |
|---|---|
| `palinode_search` | Semantic + keyword search with optional category filter |
| `palinode_save` | Save a memory (content, type, optional metadata) |
| `palinode_ingest` | Fetch a URL and save as a research reference |
| `palinode_status` | Health check — file counts, index stats, service status |
| `palinode_history` | Retrieve git history for a specific memory file |
| `palinode_entities` | Search associative graph by entity or cross-reference |
| `palinode_consolidate` | Preview or run operation-based memory compaction |
| `palinode_diff` | Show memory changes in the last N days |
| `palinode_blame` | Trace a fact back to the session that recorded it |
| `palinode_timeline` | Show the evolution of a memory file over time |
| `palinode_rollback` | Safe commit-driven reversion of memory files |
| `palinode_push` | Sync memory to a remote git repository |
| `palinode_trigger` | Add or list prospective narrative triggers |

---

## API Reference

| Method | Path | Description |
|---|---|---|
| `GET` | `/status` | Health check + stats |
| `POST` | `/search` | `{query, category?, limit?, hybrid?}` → ranked results |
| `POST` | `/search-associative` | Associative recall via entity graph |
| `POST` | `/save` | `{content, type, slug?, entities?}` → creates memory file |
| `POST` | `/ingest-url` | `{url, name?}` → fetch + save to research |
| `GET/POST` | `/triggers` | List or add prospective triggers |
| `POST` | `/check-triggers` | Check if any triggers match a query |
| `GET` | `/history/{file_path}` | Git history for a file |
| `POST` | `/consolidate` | Run or preview compaction |
| `POST` | `/split-layers` | Split files into identity/status/history |
| `POST` | `/bootstrap-fact-ids` | Add fact IDs to existing files |
| `GET` | `/diff` | Git diff for a file |
| `GET` | `/blame` | Git blame for a file |
| `GET` | `/timeline` | Git activity over time |
| `POST` | `/rollback` | Revert a file |
| `POST` | `/push` | Push memory changes to git remote |
| `GET` | `/git-stats` | Git summary stats |
| `POST` | `/reindex` | Full rebuild of vector + BM25 indices |
| `POST` | `/rebuild-fts` | Rebuild BM25 index only |

---

## Design Philosophy

Palinode makes specific bets about how agent memory should work:

1. **Files are truth.** Not databases, not vector stores, not APIs. Markdown files that humans can read, edit, and version with git.

2. **Typed, not flat.** People, projects, decisions, insights — each has structure. This enables reliable retrieval and consolidation.

3. **Consolidation, not accumulation.** 100 sessions should produce 20 well-maintained files, not 100 unread dumps. Memory gets smaller and more useful over time.

4. **Invisible when working.** The human talks to their agent. Palinode works behind the scenes. The only visible outputs are better conversations.

5. **Graceful degradation.** Vector index down → read files directly. Embedding service down → grep. Machine off → it's a git repo, clone it anywhere.

6. **Zero taxonomy burden.** The system classifies. The human reviews. If the human has to maintain a taxonomy, the system dies.

---

## Roadmap

| Phase | Status | What |
|---|---|---|
| 0 — MVP | ✅ Done | Core Python, watcher, API, SQLite-vec |
| 0.5 — Capture | ✅ Done | Plugin, dual embeddings, -es capture, inbox pipeline |
| 1 — Config + Quality | ✅ Done | Config YAML, docstrings, type hints, bug fixes |
| 1.5 — Hybrid Search | ✅ Done | BM25 + vector RRF, content-hash dedup |
| 2 — Consolidation | ✅ Done | Entity linking, temporal memory |
| 3 — Migration | ✅ Done | Mem0 backfill, automated migration pipelines |
| 4 — Git Tools | ✅ Done | Memory diffing, blame, CLI timeline, remote push |
| 5 — Compaction | ✅ Done | Operation-based compaction, layered core files |
| 5.5 — Recall+ | ✅ Done | Associative entity search, prospective triggers, temporal decay |

See [docs/ROADMAP.md](docs/ROADMAP.md) for the full research-informed roadmap.

---

## Inspirations & Acknowledgments

Palinode is informed by research and ideas from several projects in the agent memory space. We believe in attributing what we learned and borrowed.

### Architecture Inspiration

- **[OpenClaw](https://github.com/openclaw/openclaw)** — The plugin SDK, lifecycle hooks, and `MEMORY.md` pattern that Palinode extends and replaces. Palinode started as a better memory system for OpenClaw agents.

- **[memsearch](https://zilliztech.github.io/memsearch/) (Zilliz)** — Hybrid BM25 + vector search over markdown files, content-hash deduplication, and the "derived index" philosophy (vector DB is a cache, files are truth). Palinode's Phase 1.5 hybrid search and dedup are directly inspired by memsearch's approach.

- **[Letta](https://github.com/letta-ai/letta) (formerly MemGPT)** — Tiered memory architecture (Core/Recall/Archival), agent self-editing via tools, and the MemFS concept (git-backed markdown as memory). Palinode's tiered injection and `core: true` system parallel Letta's Core Memory blocks.

- **[LangMem](https://github.com/langchain-ai/langmem) (LangChain)** — Typed memory schemas with update modes (patch vs insert), background consolidation manager, and the semantic/episodic/procedural split. Palinode's planned consolidation cron (Phase 2) follows LangMem's background manager pattern.

### Specific Ideas Borrowed

| Feature | Source | How We Adapted It |
|---|---|---|
| Hybrid search (BM25 + vector + RRF) | memsearch (Zilliz) | FTS5 + SQLite-vec with RRF merging, zero new dependencies |
| Content-hash deduplication | memsearch (Zilliz) | SHA-256 per chunk, skip Ollama calls for unchanged content |
| Tiered context injection | Letta (MemGPT) | `core: true` files always injected; summaries on non-first turns |
| Typed memory with frontmatter | LangMem + Obsidian patterns | YAML frontmatter categories, entities, status fields |
| Two-door principle | [OB1 / OpenBrain](https://github.com/NateBJones-Projects/OB1) (Nate B. Jones) | Human door (files, inbox, -es flag) + agent door (MCP, tools, API) |
| Temporal anchoring | [Zep / Graphiti](https://github.com/getzep/zep) | `last_updated` + git log for when memories changed |
| Background consolidation | LangMem | Weekly cron distills daily notes into curated memory |
| Entity cross-references | [Mem0](https://github.com/mem0ai/mem0), [Cognee](https://github.com/cognee-ai/cognee) | Frontmatter `entities:` linking files into a graph |
| Memory security scanning | [Hermes Agent](https://github.com/NousResearch/hermes-agent) (MIT) | Prompt injection + credential exfiltration blocking on save |
| FTS5 query sanitization | [Hermes Agent](https://github.com/NousResearch/hermes-agent) (MIT) | Handles hyphens, unmatched quotes, dangling booleans |
| Capacity display | Hermes Agent prompt builder pattern | `[Core Memory: N / 8,000 chars — N%]` for agent self-regulation |
| Observational memory (evaluating) | [Mastra](https://mastra.ai/research/observational-memory) | Background observer agent pattern for proactive memory updates |

### Research References

- *Memory in the Age of AI Agents: A Survey* — [TeleAI-UAGI/Awesome-Agent-Memory](https://github.com/TeleAI-UAGI/Awesome-Agent-Memory)
- Nate B. Jones — [OpenBrain Substack](https://natesnewsletter.substack.com/) on context engineering and the "two-door" principle

### What's Ours

Based on a [comprehensive landscape analysis](docs/perplexity-landscape-2026-03-31.md) (March 2026, covering Letta, LangMem, Mem0, Zep, memsearch, Hermes, OB1, Cognee, Hindsight, All-Mem, DiffMem, and others), these features are unique to Palinode:

1. **Git operations as first-class agent tools** — `palinode_diff`, `palinode_blame`, `palinode_rollback`, `palinode_push` exposed via MCP. No other production system makes git ops callable by the agent.
2. **KEEP/UPDATE/MERGE/SUPERSEDE/ARCHIVE operation DSL** — LLM proposes, deterministic executor disposes. Closest analogue is All-Mem (academic, graph-only). No production system does this on markdown.
3. **Per-fact addressability via `<!-- fact:slug -->`** — HTML comment IDs inline in markdown, invisible in rendering, preserved by git, targetable by compaction operations.
4. **4-phase injection pipeline** — Core (always) → Topic (per-turn search) → Associative (entity graph) → Triggered (prospective recall). Research confirms no system combines all four.
5. **Files survive everything** — if Ollama dies, if the API crashes, if the vector DB corrupts — `cat` and `grep` still work. The index is derived; the files are truth.

If you know of prior art we missed, please [open an issue](https://github.com/Paul-Kyle/palinode/issues).

---

## Contributing

Palinode is in active development. Issues and PRs welcome.

See the [GitHub Issues](https://github.com/Paul-Kyle/palinode/issues) for the current roadmap and open tasks.

---

## License

MIT

---

*Built by [Paul Kyle](https://github.com/Paul-Kyle) with help from AI agents who use Palinode to remember building Palinode.* 🧠
