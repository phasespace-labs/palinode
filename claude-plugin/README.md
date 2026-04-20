# Palinode Claude Code Plugin

Persistent memory for AI agents. Your agent's memory is a folder of plain markdown files, indexed for semantic search and compacted by an LLM. Every change is a git commit. If every service crashes, `cat` still works.

- **Files are the source of truth.** Not a database, not a cloud service, not a vector store. Markdown files you can read, diff, grep, and version.
- **Hybrid search.** SQLite-vec for semantic similarity + FTS5 for BM25 keyword matching, fused via reciprocal rank fusion.
- **Deterministic compaction.** An LLM proposes typed operations (keep, update, merge, supersede, archive) and a Python executor applies them. Every operation is a git commit. You can `git blame` every line in your agent's brain.
- **Local-first.** Runs entirely on your machine. BGE-M3 embeddings via local Ollama. No data leaves your machine unless you configure it to.

## Prerequisites

Palinode is a **local-first** memory system. The Claude Code plugin is a thin configuration layer — it tells Claude Code how to connect to a `palinode-mcp` server running on your machine. You need to install and run the Palinode server yourself before the plugin is useful.

### 1. Python 3.11 or newer

Palinode requires Python 3.11+. Check with:

```bash
python3 --version
```

### 2. Ollama with the BGE-M3 embedding model

Palinode uses BGE-M3 (1024-dim embeddings) via Ollama for semantic search. Install Ollama from [ollama.com/download](https://ollama.com/download) then pull the model:

```bash
ollama pull bge-m3
```

Ollama runs a local daemon on `http://localhost:11434`. Verify it's running:

```bash
curl http://localhost:11434/api/tags
```

### 3. Install the Palinode Python package

Install from source (PyPI publish coming soon):

```bash
git clone https://github.com/phasespace-labs/palinode.git
cd palinode
pip install -e .
```

This installs four CLI binaries:
- `palinode` — the main CLI (26 commands)
- `palinode-api` — the REST API server (port 6340)
- `palinode-watcher` — the file indexer daemon
- `palinode-mcp` — the MCP server (stdio, what the plugin launches)

### 4. Run the Palinode services

Palinode has two background services that need to be running: `palinode-api` (the REST API the MCP server talks to) and `palinode-watcher` (watches your memory directory for changes and reindexes).

**Quick start (foreground, for testing):**

In one terminal:

```bash
export PALINODE_DIR=~/palinode
mkdir -p ~/palinode
palinode-api
```

In a second terminal:

```bash
export PALINODE_DIR=~/palinode
palinode-watcher
```

**Persistent (systemd user services on Linux):**

Example service files are in `systemd/` in the main repository. Copy them to `~/.config/systemd/user/`, run `systemctl --user daemon-reload`, then enable and start:

```bash
systemctl --user enable --now palinode-api palinode-watcher
```

**macOS launchd** and **Windows** equivalents are left as an exercise for the reader; the services are standard Python processes that can be managed by any supervisor.

## Installing the plugin

Once Palinode is installed and the services are running, install this Claude Code plugin:

```
/plugin install palinode@phasespace-labs
```

Or, during development, point Claude Code at this directory directly:

```bash
claude --plugin-dir /path/to/palinode/claude-plugin
```

The plugin configures Claude Code to launch `palinode-mcp` as a stdio MCP server whenever a session starts. The MCP server is a thin HTTP client that talks to the `palinode-api` REST server you're already running.

## What you get

Once installed and connected, the plugin exposes 17 MCP tools to Claude Code:

### Search and retrieval
- `palinode_search` — hybrid semantic + keyword search across all memory files
- `palinode_status` — instance health, index size, embedding model, tier 2a queue depth
- `palinode_list` — list memory files by category or core-only
- `palinode_read` — read a specific memory file with optional frontmatter parsing
- `palinode_entities` — entity graph traversal (people, projects, decisions)

### Save and capture
- `palinode_save` — write a new memory item. Supports type, entities, core flag, slug, source. As of v0.6.0, saves run write-time contradiction checking in the background.
- `palinode_session_end` — capture session outcomes (summary, decisions, blockers) to daily notes and project status files
- `palinode_ingest` — fetch a URL and store it as a research reference

### Provenance and audit
- `palinode_history` — git history of a memory file with diff stats and rename tracking
- `palinode_blame` — trace every line in a memory file back to the session that wrote it
- `palinode_diff` — what changed across the whole memory in the last N days
- `palinode_rollback` — safely revert a memory file to a prior version

### Consolidation and maintenance
- `palinode_consolidate` — run compaction manually (useful for `--dry-run` inspection)
- `palinode_lint` — scan memory for orphans, stale active files, missing frontmatter, potential contradictions
- `palinode_push` — push memory repo to a remote (backup and cross-machine sync)

### Triggers and prompts
- `palinode_trigger` — register prospective triggers that inject memory files when context matches
- `palinode_prompt` — list, read, and activate versioned LLM prompt files

## What this plugin is NOT

- **Not a hosted service.** Palinode runs entirely on your machine. If you want a hosted version, that's coming but not yet available.
- **Not a replacement for your existing notes app.** Palinode is an agent memory layer, not a personal knowledge management system. It works alongside Obsidian, Logseq, or any other markdown tool that reads the same files.
- **Not a cloud backup.** Use `palinode push` to back up to a remote git repo you control.
- **Not a compliance tool.** Palinode has audit trails (git blame every line) but is not SOC2/HIPAA/etc. certified. Use at your own risk for regulated workloads.

## Troubleshooting

### "palinode-mcp: command not found"

The MCP server binary isn't on your PATH. Re-run `pip install -e .` from the Palinode source directory, or check that your Python environment's `bin/` is on PATH.

### "Failed to connect to Palinode API"

`palinode-mcp` (stdio) talks to `palinode-api` (HTTP on port 6340). Make sure `palinode-api` is running. Check with:

```bash
curl http://localhost:6340/status
```

If that returns a JSON status object with `ollama_reachable: true`, you're good. If `ollama_reachable: false`, the API can't reach Ollama — make sure the Ollama daemon is up.

### "Embedder error: connection refused"

Ollama isn't running or isn't reachable at `http://localhost:11434`. Start Ollama (`ollama serve` in a terminal, or the macOS app).

### Search returns nothing

Run `palinode_status` and check `total_files` and `fts_chunks`. If both are 0, the watcher hasn't indexed anything yet — create a memory file in `~/palinode/projects/example.md` and wait a few seconds for the watcher to pick it up.

## Learn more

- [Main repository](https://github.com/phasespace-labs/palinode)
- [CHANGELOG](https://github.com/phasespace-labs/palinode/blob/main/docs/CHANGELOG.md) for what's in v0.6.1
- [Compaction demo](https://github.com/phasespace-labs/palinode/tree/main/examples/compaction-demo) — walkthrough of a memory file across three consolidation passes with blame + diff output

## License

MIT. See [LICENSE](https://github.com/phasespace-labs/palinode/blob/main/LICENSE) in the main repository.
