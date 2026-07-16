<!-- mcp-name: io.github.phasespace-labs/palinode -->

```
┌─ palinode ─┐
│ ░░░░░░░░░░ │
│ ▓▓▓▓▓▓▓▓▓▓ │
│ ██████████ │
└────────────┘
```

**Audit-grade memory for AI coding agents. Git-versioned and file-native — every fact your agent recalls, you can `blame`, `diff`, and `rollback`. MCP-first, so one memory works in every editor.**

Agent memory is becoming a commodity. *Auditable* agent memory is not: Palinode is the memory layer where every remembered fact is a line in a git-versioned markdown file — readable, diffable, attributable to the commit that recorded it, and revertible when it's wrong. No black-box vector store you have to trust.

Your agent's memory is a folder of markdown files. Palinode indexes them with hybrid search, compacts them with an LLM, and serves them through MCP — so the same memory works in Claude Code, Cursor, Windsurf, Zed, VS Code (Continue/Cline), and any other MCP-compatible editor. Bring your own Obsidian vault, or use Palinode as one: `palinode init --obsidian /path/to/vault` scaffolds a full vault with graph defaults, daily-notes wiring, and an LLM-maintained wiki contract. Enterprises can govern AI memory the same way they govern code. If every service crashes, `cat` still works.

*A palinode is a poem that retracts what was said before and says it better. That's what memory compaction does.*

---

## Supported Platforms

| Platform | Session Skill Path | MCP Config |
|----------|--------------------|------------|
| **Claude Code CLI** | `~/.claude/skills/` | `~/.claude.json` |
| **Claude Desktop** | `~/.claude/skills/` | `claude_desktop_config.json` |
| **Cursor** | `.cursor/skills/` | `.cursor/mcp.json` |
| **VS Code + Claude** (Continue / Cline) | `~/.claude/skills/` | see [MCP-INSTALL-RECIPES.md](docs/MCP-INSTALL-RECIPES.md) |
| **JetBrains + Claude** | `~/.claude/skills/` | `~/.claude.json` |
| **Antigravity IDE** | `.agent/skills/` | native 3-dot MCP menu |
| **Codex CLI** | N/A (no skills) | `~/.codex/config.toml` |

All platforms share the same MCP server — install once on your server, connect from any IDE. See [docs/MCP-SETUP.md](docs/MCP-SETUP.md) and [docs/MCP-INSTALL-RECIPES.md](docs/MCP-INSTALL-RECIPES.md) for per-client config snippets.

---

## The Idea

Most agent memory is a black box. You can't read it, you can't diff it, you can't `grep` it when the vector DB is down. Palinode bets on **plain files as the source of truth** and builds everything else as a derived index.

```
Files (markdown + YAML frontmatter)
  ↓ watched
Index (SQLite-vec vectors + FTS5 keywords, single .db file)
  ↓ queried by
Interfaces (MCP server, REST API, CLI, OpenClaw plugin)
  ↓ compacted by
LLM (proposes ops → deterministic executor applies them → git commits)
```

That's the whole architecture. One directory of `.md` files, one SQLite database, one API server. No Postgres, no Redis, no cloud dependency.

---

## One Backend, Every Interface

Palinode doesn't care how you talk to it. The full toolkit — save, search, doctor, dedup-suggest, orphan-repair, diff, blame, rollback, and more — works through every interface:

| Interface | Transport | Best For |
|-----------|-----------|----------|
| **MCP Server** | Streamable HTTP or stdio | Claude Code, Claude Desktop, Cursor, Windsurf, Zed, VS Code (Continue/Cline) |
| **REST API** | HTTP on :6340 | Scripts, webhooks, custom integrations |
| **CLI** | Wraps REST API | Cron jobs, SSH, shell scripts (8x fewer tokens than MCP) |
| **Plugin** | OpenClaw lifecycle hooks | Agent frameworks with inject/extract patterns |

Set up once on a server. Connect from any machine, any IDE, any agent framework. The MCP server is a pure HTTP client — it holds no state, no database connection, no embedder. Point it at the API and go.

```json
{
  "mcpServers": {
    "palinode": { "type": "http", "url": "http://your-server:6341/mcp/" }
  }
}
```

That's the entire client config. Works with Claude Code, Claude Desktop, Cursor, Windsurf, Zed, and VS Code (Continue/Cline). `palinode-mcp-sse` serves **streamable-HTTP** at `/mcp/` — the binary name is historical; use `"type": "http"`, not `"type": "sse"`. Always include the trailing slash in the URL. See [docs/MCP-SETUP.md](docs/MCP-SETUP.md) for editor-specific install recipes.

---

## How It Works

**Store** — Typed markdown files (people, projects, decisions, insights) with YAML frontmatter. Git-versioned. Human-readable. Editable in Obsidian, VS Code, vim, or anything.

**Index** — A file watcher embeds with BGE-M3 and indexes with FTS5 as you save. Content-hash dedup skips re-embedding unchanged files (~90% savings). Single SQLite file, zero external services.

**Search** — Hybrid BM25 + vector search merged with Reciprocal Rank Fusion. Keyword precision when you need exact terms, semantic recall when you don't. Optional associative entity graph and prospective triggers.

**Compact** — Weekly consolidation where an LLM proposes structured operations (KEEP / UPDATE / MERGE / SUPERSEDE / ARCHIVE) and a deterministic executor applies them. The LLM never touches your files directly. Every compaction is a git commit you can review, blame, or revert.

**Audit** — `git blame` any fact. `git diff` any change. `rollback` any mistake. These aren't just git-compatible files — `palinode_diff`, `palinode_blame`, and `palinode_rollback` are first-class tools your agent can call.

---

## Requirements

- **Python 3.11+**
- **Git**
- **Ollama** with `bge-m3` (`ollama pull bge-m3`, ≈1.2 GB) — for semantic search.
  **Optional:** without an embedder Palinode runs in **keyword-only mode** (BM25/FTS5) — save,
  search, and audit all still work; you just don't get vector recall until you add one.

Optional extras: a chat model for weekly consolidation (any 7B+ that outputs JSON), OpenClaw for agent plugin hooks.

---

## Install

One path — clone, install, point at a memory directory, check it worked:

```bash
# 1. Get the code (lives separately from your memory — never the same directory)
git clone https://github.com/phasespace-labs/palinode ~/palinode-src && cd ~/palinode-src
python3 -m venv venv && source venv/bin/activate
pip install -e .

# 2. Create your memory directory (this is where your data lives — keep it private)
mkdir -p ~/.palinode && cd ~/.palinode && git init
cp ~/palinode-src/palinode.config.yaml.example palinode.config.yaml
# memory_dir stays commented out in the copied config → it inherits PALINODE_DIR below

# 3. Start the services (each in its own terminal — or as a service, next section)
PALINODE_DIR=~/.palinode palinode-api        # REST API on :6340
PALINODE_DIR=~/.palinode palinode-watcher     # auto-indexes on file save

# 4. Did it work?
palinode doctor
```

`palinode doctor` is the single "did it install correctly?" command — run it after every install, upgrade, or server move. On a fresh venv, use the venv's absolute path for the MCP command (`~/palinode-src/venv/bin/palinode-mcp`) so it resolves its own dependencies — see [llms-install.md](llms-install.md) for the wrong-Python trap.

> Your memory directory is **private** — it holds personal data. Never make it public; the code repo contains zero memory files. For a pre-populated demo, copy `examples/sample-memory/` into `~/.palinode/`.

---

## Running as a service

Three long-running processes (API, watcher, embedder) shouldn't live in terminal tabs. Pick one:

| Platform | How | Details |
|---|---|---|
| **Anywhere with Docker** | `docker compose up -d` from the repo root — API + watcher + Ollama, with the `bge-m3` pull handled for you (≈1.2 GB on first run) | [docker-compose.yml](docker-compose.yml) header comments |
| **Linux** | systemd units via `deploy/systemd/install.sh --enable` | [deploy/systemd/README.md](deploy/systemd/README.md) |
| **macOS** | launchd LaunchAgents from templates | [deploy/launchd/README.md](deploy/launchd/README.md) |
| **Windows** | use Docker Compose (set `PALINODE_DATA_DIR` to a Windows path) | [docker-compose.yml](docker-compose.yml) header comments |

With compose, your memory stays on the **host** at `~/.palinode` (override with `PALINODE_DATA_DIR`) — the containers mount it; files remain the source of truth. Already running Ollama on the host? `OLLAMA_URL=http://host.docker.internal:11434 docker compose up -d palinode-api palinode-watcher` skips the bundled one. Verify any of the three the same way: `palinode doctor` (or `curl http://127.0.0.1:6340/status`).

---

## Connect your editor

Palinode speaks MCP. Generate the correct config with the CLI instead of hand-pasting JSON (which drifts):

| Editor / harness | One command |
|---|---|
| **Claude Code (CLI)** | `claude mcp add palinode -- palinode-mcp` |
| **Claude Desktop** | `palinode mcp-config --stdio` → paste into the config it prints (quit Desktop first) |
| **Cursor / Windsurf / other MCP clients** | `palinode mcp-config --stdio` (local) or `--http` (remote / streamable-HTTP) |
| **Diagnose an existing setup** | `palinode mcp-config --diagnose` |

`palinode mcp-config` never writes your editor's config file — it prints a ready-to-paste block (pipe it straight to the target). Full per-harness detail: [docs/MCP-INSTALL-RECIPES.md](docs/MCP-INSTALL-RECIPES.md).

---

## Daily use — drop into a project

Already installed with `palinode-api` running? Scaffold any project in one command:

```bash
cd your-project
palinode init
```

That scaffolds `.claude/CLAUDE.md` (memory instructions, appended if one exists), `.claude/settings.json` (a `SessionEnd` hook that auto-captures on `/clear`, logout, and exit), the hook script, and `.mcp.json` (points Claude Code at the `palinode` MCP server). Your agent then searches prior context on startup, saves decisions as you work, and snapshots the session on `/clear`. Re-run with `--dry-run` to preview, `--force` to overwrite, or `--no-mcp` / `--no-hook` to scope it.

---

## Usage Examples

### Save a decision, recall it later

```bash
# During a session — save a decision
palinode save --type Decision "Chose SQLite over Postgres for the cache layer. \
  Reason: no ops burden, single-file deployment, good enough for our scale."

# Next week — search for it
palinode search "database decision for cache"
```

### End-of-session capture

```bash
# Agent calls at end of coding session
palinode session-end \
  --summary "Migrated auth from JWT to session tokens" \
  --decisions "Session tokens stored server-side, 24h expiry" \
  --blockers "Need to update mobile client auth flow"
```

### Audit trail — who decided what and when

```bash
# Trace a fact back to when it was recorded
palinode blame decisions/auth-migration.md

# See what changed across all memory in the last week
palinode diff --days 7
```

---

## Tools

27 tools available through every interface:

| Tool | What It Does |
|------|-------------|
| `search` | Hybrid BM25 + vector search with category filter |
| `save` | Store a typed memory (person, decision, insight, project) |
| `list` | Browse memory files by type, filter by core status |
| `read` | Read the full content of a memory file |
| `ingest` | Fetch a URL and save as research |
| `status` | Health check — file counts, index stats, service status |
| `entities` | Entity graph — cross-references between memories |
| `consolidate` | Preview or run LLM-powered compaction |
| `diff` | What changed in the last N days |
| `blame` | Trace a fact back to the commit that recorded it |
| `history` | Git history for a file with diff stats and rename tracking |
| `rollback` | Revert a file to a previous commit (safe, creates new commit) |
| `push` | Sync memory to a remote git repo |
| `trigger` | Prospective recall — auto-inject when a topic comes up |
| `lint` | Health scan — orphans, stale files, missing fields |
| `session_end` | Capture summary, decisions, and blockers at end of session |
| `prompt` | List, show, or activate versioned LLM prompts |
| `dedup_suggest` | Before saving, surface existing files that overlap the draft |
| `orphan_repair` | Find semantic matches for broken `[[wikilinks]]` |
| `doctor` | Fast diagnostic pass — 18+ checks across paths, services, config, index |
| `doctor_deep` | Full diagnostic with canary write test (~10–15s) |
| `cluster_neighbors` | Find top-K semantically related files NOT already wiki-linked — surface implicit relationships for cross-link proposals |
| `topic_coverage` | Given a short topic phrase, return whether any existing wiki page already covers it (binary `covered` / `best_match` / `similarity`) |
| `depends` | Dependency tree (or unblocked-items list) from `depends_on` / `blocks` / `parallel_with` frontmatter on ProjectSnapshots |
| `timeline` | _Deprecated — use `history` with `detail='full'`._ Commit-level evolution of a file with per-commit diffs |

Every tool is accessible as `palinode_<name>` via MCP, `palinode <name>` via CLI, or `POST/GET /<name>` via the REST API.

---

## Stack

| Layer | Choice | Why |
|-------|--------|-----|
| Source of truth | Markdown + YAML frontmatter | Human-readable, git-versioned, portable |
| Vector index | SQLite-vec (embedded) | No server, single file, zero config |
| Keyword index | SQLite FTS5 (embedded) | BM25 for exact terms, zero dependencies |
| Embeddings | BGE-M3 via Ollama | Local, private, no API key needed |
| API | FastAPI | Lightweight, async, one process |
| MCP | Python MCP SDK (Streamable HTTP) | Works with every IDE over the network |
| CLI | Click (wraps REST API) | Shell-native, TTY-aware output |
| Behavior | [`PROGRAM.md`](PROGRAM.md) | What to remember, how to extract, how to compact — edit one file to change all behavior |

---

## Memory File Format

```yaml
---
id: project-palinode
category: project
name: Palinode
core: true
status: active
entities: [person/paul]
last_updated: 2026-04-05T00:00:00Z
summary: "Persistent memory for AI agents."
canonical_question: "What is Palinode and what does it do?"
---
# Palinode

Your content here. As detailed or brief as you want.
Files marked `core: true` are always in context.
Everything else is retrieved on demand via hybrid search.
The `canonical_question` field anchors the file to the question it answers, improving search relevance.
```

---

## Open in Obsidian

Palinode stores every memory as a plain markdown file — which means your memory directory is already a valid Obsidian vault. Point Obsidian at the folder and you get graph view, backlinks, and Bases on top of Palinode's hybrid search and compaction. No sync job, no plugin to install, no two-source-of-truth problem.

```bash
palinode init --obsidian ~/palinode-vault
```

This scaffolds the vault directory layout, an `_index.md` Map of Content, a `_README.md` orientation page, and an opinionated `.obsidian/` config (graph view colour-coded by category, daily-notes wired to `daily/`). Then open the directory in Obsidian.

The LLM follows a **wiki-maintenance contract** — it keeps `entities:` frontmatter and `[[wikilinks]]` in the note body in sync so the Obsidian graph stays accurate as new memories are saved. When you save a memory with entity references, Palinode appends an idempotent `## See also` block linking them as wikilinks.

Two embedding-aware tools support wiki hygiene: `palinode_dedup_suggest` checks whether a draft overlaps an existing file before creating a duplicate, and `palinode_orphan_repair` finds semantic matches for broken `[[wikilinks]]`. Both are callable via MCP, CLI, and REST.

See [docs/OBSIDIAN.md](docs/OBSIDIAN.md) for the comprehensive guide: quickstart, wiki contract details, migration paths, and FAQ.

---

## Diagnose with palinode doctor

Silent misconfiguration — a `db_path` pointing at the wrong file, a watcher indexing a stale directory, a phantom DB file — is the most common reason Palinode doesn't behave as expected after an upgrade or server move. `palinode doctor` catches this entire class of bugs.

```bash
palinode doctor
```

The command runs 18+ checks across paths, services, config consistency, index health, and disk state, and emits a structured report with a pass/warn/fail status for each. `--fix` mode applies safe automated repairs (creates missing directories, appends the CLAUDE.md Palinode block) — it never moves user data; phantom DB files and DB-path mismatches print suggested `mv` commands but never execute them.

Run `palinode doctor` after every install, upgrade, or server migration. See [docs/DOCTOR.md](docs/DOCTOR.md) for the full check catalog and `--fix` reference.

---

## Configuration

All behavior is in `palinode.config.yaml`:

```yaml
memory_dir: "~/.palinode"
ollama_url: "http://localhost:11434"
embedding_model: "bge-m3"

recall:
  search:
    top_k: 5
    threshold: 0.4
  core:
    max_chars_per_file: 3000

search:
  hybrid_enabled: true
  hybrid_weight: 0.5         # 0.0 = vector only, 1.0 = BM25 only

consolidation:
  llm_model: "llama3.1:8b"   # any chat model that outputs JSON
  llm_url: "http://localhost:11434"
  llm_fallbacks:              # tried in order if primary fails
    - model: "qwen2.5:14b-instruct"
      url: "http://localhost:11434"
```

All models are swappable. Any Ollama embedding model, any OpenAI-compatible chat endpoint. See [palinode.config.yaml.example](palinode.config.yaml.example) for the full reference.

When exposing the API beyond loopback, set `PALINODE_API_TOKEN` — see [SECURITY.md](SECURITY.md#api-authentication) for the bearer-token auth model and the public-bind startup gate.

---

## API Reference

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/status` | Health check + stats |
| `POST` | `/search` | Hybrid search with filters |
| `POST` | `/search-associative` | Entity graph traversal |
| `POST` | `/save` | Create a typed memory file. Schema: `{content, type, slug?, entities?, title?}`. Body cap **5 MB** (override via `PALINODE_MAX_REQUEST_BYTES`). |
| `POST` | `/ingest-url` | Fetch URL, save as research |
| `GET/POST` | `/triggers` | Prospective recall triggers |
| `POST` | `/consolidate` | Run or preview compaction |
| `GET` | `/list` | Browse files by type |
| `GET` | `/read?file_path=...` | Read a memory file |
| `GET` | `/history/{file_path}` | Git log for a file |
| `GET` | `/diff` | Recent changes |
| `GET` | `/blame/{file_path}` | Git blame |
| `POST` | `/rollback` | Revert a file |
| `POST` | `/push` | Push to git remote |
| `POST` | `/reindex` | Rebuild indices |
| `POST` | `/session-end` | Capture session summary |
| `POST` | `/lint` | Health scan |

---

## Design Principles

1. **Files are truth.** Not databases, not vector stores. Markdown files that humans can read, edit, and version with git.

2. **Typed, not flat.** People, projects, decisions, insights — each has structure. This enables reliable retrieval and consolidation.

3. **Consolidation, not accumulation.** 100 sessions should produce 20 well-maintained files, not 100 unread dumps.

4. **Invisible when working.** The human talks to their agent. Palinode works behind the scenes.

5. **Graceful degradation.** Vector index down? Read files directly. Embedding service down? Grep. Machine off? It's a git repo, clone it anywhere.

6. **Zero taxonomy burden.** The system classifies. The human reviews. If the human has to maintain a taxonomy, the system dies.

---

## What's Unique

- **Your data, your files** — No accounts, no cloud dependency, no vendor lock-in. Your memory is markdown files in a directory you control. Export is `cp`. Backup is `git push`. Whatever happens to any tool in this ecosystem, your data is plain text on your filesystem.
- **Cross-IDE memory** — Your memory lives in one place. Connect from Claude Code, Cursor, Windsurf, Zed, or any MCP-compatible editor. Switch IDEs without losing context.
- **Git operations as agent tools** — `diff`, `blame`, `rollback`, `push` exposed via MCP. No other system makes git ops callable by the agent.
- **Operation-based compaction** — KEEP/UPDATE/MERGE/SUPERSEDE/ARCHIVE DSL. The LLM proposes structured operations and the deterministic executor applies them. Every compaction is a reviewable git commit.
- **Per-fact addressability** — `<!-- fact:slug -->` IDs inline in markdown, invisible in rendering, preserved by git, targetable by compaction.
- **4-phase injection** — Core (always) + Topic (per-turn search) + Associative (entity graph) + Triggered (prospective recall).
- **Multi-transport MCP** — stdio for local, Streamable HTTP for remote. One server, any IDE on any machine.
- **If everything crashes, `cat` still works.**

---

## Acknowledgments

Palinode builds on ideas from [Karpathy's LLM Knowledge Bases](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f), [Letta](https://github.com/letta-ai/letta) (tiered memory), and [LangMem](https://github.com/langchain-ai/langmem) (typed schemas + background consolidation). See [docs/ACKNOWLEDGMENTS.md](docs/ACKNOWLEDGMENTS.md) for the full list.

See also the [epistemic integrity discussion](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) in the Karpathy gist thread — particularly the problem of LLM wikis that "synthesise without citing, drift from sources without knowing it, and present false certainty where disagreement exists." Git-based provenance is Palinode's answer to that problem.

If you know of prior art we missed, please [open an issue](https://github.com/phasespace-labs/palinode/issues).

---

## License

MIT — [Privacy Policy](PRIVACY.md)

---

*Built by [Paul Kyle](https://github.com/Paul-Kyle) with help from AI agents who use Palinode to remember building Palinode.*
