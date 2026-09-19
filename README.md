<!-- mcp-name: io.github.phasespace-labs/palinode -->

```
┌─ palinode ─┐
│ ░░░░░░░░░░ │
│ ▓▓▓▓▓▓▓▓▓▓ │
│ ██████████ │
└────────────┘
```

**Inspectable, correctable project memory for repeated work across sessions, agents, and worktrees.**

Last week, your team chose SQLite for a cache because it avoided operating a
database. This week, a changed requirement makes PostgreSQL the better choice.
Without the decision and its reason, a fresh agent can repeat the old approach.
With Palinode, you can save the replacement, inspect the original and its history,
and correct the record for the next session.

Palinode keeps project memory in Markdown files and Git history under your local
control. An agent can use its tools to save, search, and inspect that memory.
Some supported client integrations can also perform configured capture or recall;
their automatic behavior, scope, and controls vary by client and configuration.
Nothing is silently promoted from an unmarked note to a fact.

*A palinode is a poem that retracts what was said before and says it better. That's what memory compaction does.*

Built by [Paul Kyle](https://github.com/Paul-Kyle) at [phasespace-labs](https://github.com/phasespace-labs). See [AUTHORS](AUTHORS.md).

---

## Start here

Follow the [canonical Quickstart](docs/QUICKSTART.md) for one complete journey:
install → start → connect an MCP client → save a decision → open a fresh session
→ inspect its markdown/provenance → correct it. It keeps the private memory
store separate from the code checkout and distinguishes the current release
from source-checkout capabilities.

### Inspect the retired decision

![The fictional Harbor Notes inspector after the initial SQLite decision has been archived; the provenance panel includes the Saved commit.](docs/images/inspector-harbor-notes.png)

The screenshot shows the **archived** initial SQLite decision after the separate
PostgreSQL replacement was saved and the original was retired. See the
[local provenance UI guide](docs/UI.md) for the full read-only inspector and
the correction lifecycle; the guide explains that the image is taken after
retirement, not before the save steps.

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
| **Pi** | N/A (native extension) | [plugins/pi/](plugins/pi/) — per-turn recall via lifecycle hooks |
| **Cline CLI / SDK** | N/A (native plugin) | [plugins/cline/](plugins/cline/) — per-turn recall via `AgentPlugin` hooks |

All platforms share the same MCP server — install once on your server, connect from any IDE. **[docs/HARNESSES.md](docs/HARNESSES.md) is the cross-harness map**: what each harness gets (native hooks vs. plugin vs. MCP), how the tiers stack, and where to start. Per-client config snippets: [docs/MCP-SETUP.md](docs/MCP-SETUP.md) and [docs/MCP-INSTALL-RECIPES.md](docs/MCP-INSTALL-RECIPES.md).

---

## The Idea

Palinode treats **plain files as the source of truth** and builds its index and
interfaces from those files.

```
Files (markdown + YAML frontmatter)
  ↓ watched
Index (SQLite-vec vectors + FTS5 keywords, single .db file)
  ↓ queried by
Interfaces (MCP server, REST API, CLI, OpenClaw plugin)
  ↓ compacted by
Consolidation (structured operations → validation and application → git commits)
```

That's the whole architecture. One directory of `.md` files, one SQLite database, one API server. No Postgres, no Redis, no cloud dependency.

---

## One Backend, Every Interface

Palinode doesn't care how you talk to it. The full toolkit — save, search, doctor, dedup-suggest, orphan-repair, diff, blame, rollback, and more — works through every interface:

| Interface | Transport | Best For |
|-----------|-----------|----------|
| **MCP Server** | Streamable HTTP or stdio | Claude Code, Claude Desktop, Cursor, Windsurf, Zed, VS Code (Continue/Cline) |
| **REST API** | HTTP on :6340 | Scripts, webhooks, custom integrations |
| **CLI** | Wraps REST API | Cron jobs, SSH, shell scripts |
| **Plugin** | OpenClaw lifecycle hooks | Agent frameworks with inject/extract patterns |

Set up once on a server. Connect from any machine, any IDE, any agent framework. The MCP server is a pure HTTP client — it holds no state, no database connection, no embedder. Point it at the API and go.

```json
{
  "mcpServers": {
    "palinode": { "type": "http", "url": "http://your-server:6341/mcp/" }
  }
}
```

That's the entire client config. Works with Claude Code, Claude Desktop, Cursor, Windsurf, Zed, and VS Code (Continue/Cline). `palinode-mcp-http` serves **streamable-HTTP** at `/mcp/` — use `"type": "http"`, not `"type": "sse"`. Always include the trailing slash in the URL. See [docs/MCP-SETUP.md](docs/MCP-SETUP.md) for editor-specific install recipes.

---

## How It Works

**Store** — Typed markdown files (people, projects, decisions, insights) with YAML frontmatter. Git-versioned. Human-readable. Editable in Obsidian, VS Code, vim, or anything.

**Index** — A file watcher indexes with FTS5 as you save. Content-hash dedup skips re-embedding unchanged files. The index is a local SQLite file; embeddings can use a local BGE-M3 endpoint or a configured remote provider.

**Search** — Hybrid BM25 + vector search merged with Reciprocal Rank Fusion. The two arms have different jobs: on full-sentence questions the vector arm can retrieve semantic matches while BM25 catches exact terms and identifiers. See [benchmarks](docs/BENCHMARKS.md) for the evaluation context and limitations. Optional associative entity graph and prospective triggers.

**Compact** — Weekly consolidation where an LLM returns structured operations and Palinode validates and applies them. Every compaction is a git commit you can review, blame, or revert.

**Dream** — If you've met "dreaming" as the name for this, `palinode dream` is an alias for `palinode consolidate`. Use `--dry-run` to inspect the proposed operations; each completed pass lands as a git commit, so a bad consolidation is a diff to review and a commit to revert.

**Audit** — `git blame` any fact. `git diff` any change. `rollback` any mistake. These aren't just git-compatible files — `palinode_diff`, `palinode_blame`, and `palinode_rollback` are first-class tools your agent can call.

---

## Requirements

- **Python 3.11+**
- **Git**
- **Ollama** with `bge-m3` (`ollama pull bge-m3`, ≈1.2 GB), or another supported
  embedding endpoint — for hybrid indexing and search. A v0.21-capable source
  checkout can instead use explicit lexical mode; it is keyword/FTS retrieval,
  not a fallback when hybrid's endpoint fails. Saves persist without an
  embedder, but hybrid search returns HTTP 503 until it is reachable
  (`palinode resolve` degrades to keyword-only and says so). See the
  [Homebrew setup guide](docs/HOMEBREW.md) for installation and verification.

Optional extras: a chat model for weekly consolidation (any 7B+ that outputs JSON), OpenClaw for agent plugin hooks.

---

## Install

**Before your first capture:** Palinode stores readable Markdown and Git history. `private`/`restricted` control discovery by scope; a caller with API access can still read a hidden memory by its known path and use full-store maintenance tools. These labels provide no encryption or per-user/per-agent authentication. Protect the store, backups and API credentials; use separate instances or filesystem permissions for stronger separation. See the [privacy contract](docs/PRIVACY.md).

The [Quickstart](docs/QUICKSTART.md) is the authoritative first-use sequence.
It covers the separate private store, second-terminal environment, supported
lexical preview and hybrid model paths, editor connection, fresh-session check,
inspector, and correction workflow. Do not combine a code checkout and a
memory store. For a released Homebrew installation, begin with
[docs/HOMEBREW.md](docs/HOMEBREW.md); for an autonomous agent bootstrap, see
[llms-install.md](llms-install.md).

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

Palinode speaks MCP. Follow the connection step in the
[Quickstart](docs/QUICKSTART.md): v0.21 generates a native fragment for the
selected client, while the current release's recipes retain the supported
manual configuration. `palinode mcp-config` is read-only; it never edits a
client configuration file. Full per-harness detail:
[docs/MCP-INSTALL-RECIPES.md](docs/MCP-INSTALL-RECIPES.md).

Merge the Palinode entry into existing settings; redirecting generated output
onto an existing configuration file would overwrite it.

---

## Daily use — drop into a project

Already installed with `palinode-api` running? Scaffold any project in one command:

```bash
cd your-project
palinode init
```

That scaffolds `.claude/CLAUDE.md` (memory instructions, appended if one exists), `.claude/settings.json` (`SessionStart` + `SessionEnd` + `UserPromptSubmit` hook registration), all three hook scripts, and `.mcp.json` (points Claude Code at the `palinode` MCP server). Sessions then start smart, recall as they go, and end captured: the `SessionStart` hook injects your `core: true` memories into every fresh session (startup and `/clear`) so standing context is there before the first prompt; the `UserPromptSubmit` hook recalls relevant memory before each prompt — prospective triggers plus a strict-threshold search, injected as compact snippets, silent when nothing matches; and the `SessionEnd` hook auto-captures on `/clear`, logout, and exit. Re-run with `--dry-run` to preview, `--force` to overwrite, or `--no-mcp` / `--no-hook` to scope it. See [`examples/hooks/`](examples/hooks/) for tuning knobs.

Projects that use other harnesses get the same memory instructions automatically: when `AGENTS.md` (or a `.agent/` directory) exists, `init` appends a harness-neutral memory block to `AGENTS.md` (read by Codex, Antigravity, and other `AGENTS.md`-aware agents), and when a `.cursor/` directory exists it writes `.cursor/rules/palinode.md` for Cursor. Force or skip with `--agents`/`--no-agents` and `--cursor`/`--no-cursor` — same recall/save/session-end contract, minus the Claude-Code-only machinery (`/clear`, `/wrap`, hooks).

---

## Usage Examples

A few common flows. Every command and option is in [docs/CLI.md](docs/CLI.md).

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

# Compose the full provenance lineage of a fact — sources, saved/changed
# commits, supersession trail, typed links, and recall — in one view
palinode trace decisions/auth-migration.md

# See what changed across all memory in the last week
palinode diff --days 7

# Retire a memory that turned out to be wrong — it leaves recall but stays
# on disk, in git, and in the index. Never a hard delete.
palinode archive insights/stale-finding.md --reason "superseded by the re-run" \
  --superseded-by insights/corrected-finding.md
```

---

## Tools

Tools available through every interface (the full inventory, with parameters, is
the table in [docs/MCP-SETUP.md](docs/MCP-SETUP.md) — a prose count here only
drifts):

| Tool | What It Does |
|------|-------------|
| `session_init` | Session-start context digest for the resolved project scope |
| `search` | Hybrid BM25 + vector search with category filter; `resolve` attaches bounded evidence and a resolution per hit |
| `resolve` | What memory holds *right now* for a question or one record — what stands, what replaced what, conflicts with both sides intact, and what is explicitly unknown |
| `save` | Store a typed memory (person, decision, insight, project) |
| `list` | Browse memory files by type, filter by core status |
| `read` | Read the full content of a memory file |
| `ingest` | Fetch a URL and save as research |
| `status` | Health check — file counts, index stats, service status |
| `entities` | Entity graph — cross-references between memories |
| `consolidate` | Preview or run LLM-powered compaction |
| `archive` | Retire one memory that's wrong or obsolete — archive it, or supersede it with a named replacement |
| `restore` | Bring an archived memory back into default recall — the inverse of `archive` |
| `unretract` | Withdraw one preference's mention-level retraction from one memory |
| `forget_withdraw` | Take a forget request back — restore what it archived, un-strike what it retracted |
| `archive_expired` | Archive ephemeral memories whose TTL has expired |
| `diff` | What changed in the last N days |
| `blame` | Trace a fact back to the commit that recorded it |
| `trace` | Compose a fact's full provenance lineage — sources, saved/changed commits, supersession, typed links, recall |
| `history` | Git history for a file with diff stats and rename tracking |
| `rollback` | Revert a file to a previous commit (safe, creates new commit) |
| `push` | Sync memory to a remote git repo |
| `trigger` | Prospective recall — auto-inject when a topic comes up |
| `lint` | Health scan — orphans, stale files, missing fields |
| `review` | Advisory project-memory review that proposes corrective operations without writing them |
| `session_end` | Capture summary, decisions, and blockers at end of session |
| `prompt` | List, show, or activate versioned LLM prompts |
| `dedup_suggest` | Before saving, surface existing files that overlap the draft |
| `orphan_repair` | Find semantic matches for broken `[[wikilinks]]` |
| `doctor` | Fast diagnostic pass — 18+ checks across paths, services, config, index |
| `doctor_deep` | Full diagnostic with canary write test (~10–15s) |
| `cluster_neighbors` | Find top-K semantically related files NOT already wiki-linked — surface implicit relationships for cross-link proposals |
| `topic_coverage` | Given a short topic phrase, return whether any existing wiki page already covers it (binary `covered` / `best_match` / `similarity`) |
| `depends` | Dependency tree (or unblocked-items list) from `depends_on` / `blocks` / `parallel_with` frontmatter on ProjectSnapshots |

Every tool is accessible as `palinode_<name>` via MCP, `palinode <name>` via CLI (hyphenated: `palinode archive-expired`; `session_init` is `palinode prime`; `doctor_deep` has no separate CLI command), or `POST/GET /<name>` via the REST API.

The CLI has more commands than the tool list — service control, migration, repair, and wiki-maintenance helpers. **[docs/CLI.md](docs/CLI.md) is the full command reference**, one entry per command with options, defaults, and output behaviour.

---

## Stack

| Layer | Choice | Why |
|-------|--------|-----|
| Source of truth | Markdown + YAML frontmatter | Human-readable, git-versioned, portable |
| Vector index | SQLite-vec (embedded) | No server, single file, zero config |
| Keyword index | SQLite FTS5 (embedded) | BM25 for exact terms, zero dependencies |
| Embeddings | BGE-M3 via Ollama, or any OpenAI-compatible `/v1/embeddings` server | Local, private, no API key needed |
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
entities: [person/alice]
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
palinode init --obsidian --dir ~/palinode-vault
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

All models are swappable. Any Ollama embedding model, any OpenAI-compatible chat endpoint. The default search floors (`search.mcp_threshold=0.4` and `search.api_threshold=0.5`) were measured against real `bge-m3` embeddings; if you change the embedding model, re-check those floors and review any trigger threshold separately — the trigger default was not part of this calibration.

To re-check the search floors against your configured embedding endpoint, run
`python -m bench.abstention`. It measures false positives for no-answer queries
and retention of true results for answer-present controls as per-arm search
thresholds increase; it does not calibrate trigger thresholds. See
[palinode.config.yaml.example](palinode.config.yaml.example) for the full
reference.

**Embeddings without Ollama.** llama.cpp (`llama-server --embedding`), vLLM, and LM Studio all expose the OpenAI-compatible `/v1/embeddings` shape; select it with `dialect: openai` (default `ollama`, so existing setups are unchanged). Retry, circuit breaker, and per-input error handling are identical to the Ollama path. The Ollama tag `bge-m3` is not a llama-server model name — point llama-server at a BGE-M3 GGUF instead:

```yaml
embeddings:
  primary:
    dialect: openai
    url: "http://localhost:8080"   # a trailing /v1 is fine too
    model: "bge-m3"                # llama-server ignores it; vLLM / LM Studio match it
    dimensions: 1024
```

Hosted OpenAI-compatible embedding providers can also use bearer
authentication and provider-specific endpoint paths. Credentials stay out of
YAML: set `PALINODE_EMBEDDING_API_KEY`, or set
`PALINODE_EMBEDDING_API_KEY_FILE` to the path of a file containing the key.

For providers whose embedding endpoint is not `/v1/embeddings`, set
`endpoint_path`. When it is omitted, the existing bare-host and trailing-`/v1`
behavior is unchanged.

```yaml
embeddings:
  primary:
    dialect: openai
    url: "https://generativelanguage.googleapis.com/v1beta/openai"
    endpoint_path: "/embeddings"
    model: "gemini-embedding-001"
```

When exposing the API beyond loopback (`PALINODE_API_HOST` other than `127.0.0.1`), set `PALINODE_API_TOKEN` — the server refuses to start unauthenticated on a non-loopback bind unless you opt out explicitly with `PALINODE_API_ALLOW_UNAUTH=1`. See [SECURITY.md](SECURITY.md#api-authentication) for the bearer-token auth model and the bind gate.

---

## API Reference

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/status` | Health check + stats |
| `POST` | `/search` | Hybrid search with filters |
| `POST` | `/search-associative` | Entity graph traversal |
| `POST` | `/save` | Create a typed memory file. Schema: `{content, type, slug?, entities?, title?}`. Body cap **5 MB** (override via `PALINODE_MAX_REQUEST_BYTES`). |
| `POST` | `/ingest-url` | Fetch URL, save as research. The URL and each redirect target (five hops at most) are validated before they are requested: a host must resolve only to globally routable addresses, and the connection is made to the address that was validated rather than by name. Pinning is skipped for HTTPS requests through an HTTP CONNECT proxy; address validation still runs (see [the CLI guide](docs/CLI.md)). |
| `GET/POST` | `/triggers` | Prospective recall triggers |
| `POST` | `/consolidate` | Run or preview compaction |
| `GET` | `/list` | Browse files by type |
| `GET` | `/read?file_path=...` | Read a memory file |
| `GET` | `/history/{file_path}` | Git log for a file |
| `GET` | `/diff` | Recent changes |
| `GET` | `/blame/{file_path}` | Git blame |
| `GET` | `/trace/{file_path}` | Composed provenance lineage for a file |
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
- **Operation-based compaction** — Structured operations are schema-checked and applied as reviewable git commits.
- **Per-fact addressability** — `<!-- fact:slug -->` IDs inline in markdown, invisible in rendering, preserved by git, targetable by compaction.
- **4-phase injection** — Core (always) + Topic (per-turn search) + Associative (entity graph) + Triggered (prospective recall).
- **Multi-transport MCP** — stdio for local, Streamable HTTP for remote. One server, any IDE on any machine.
- **If everything crashes, `cat` still works.**

Measured, not asserted: [`docs/BENCHMARKS.md`](docs/BENCHMARKS.md) has LongMemEval results
with methodology, cost, and the losses.

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
