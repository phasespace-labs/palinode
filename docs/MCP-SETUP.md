# Palinode MCP Setup

Connect Palinode's MCP tools to your AI coding assistant. Two transport options:

| Transport | When to use | Key field |
|-----------|-------------|-----------|
| **Streamable HTTP** | Remote server, any IDE | `"url": "http://your-server:6341/mcp/"` |
| **stdio** | Local install, same machine | `"command": "palinode-mcp"` |

HTTP is recommended for remote setups — no SSH pipes, survives disconnects, works with every client.

**Per-client copy-pasteable install recipes** (Cursor, Windsurf, Continue, Cline, Zed) live in
[MCP-INSTALL-RECIPES.md](MCP-INSTALL-RECIPES.md). Each recipe includes the exact JSON/YAML
snippet, restart sequence, verification step, and troubleshooting table for that client.

---

## Prerequisites

- Palinode API running (`palinode-api` on port 6340)
- For HTTP transport: `palinode-mcp-sse` *(serves streamable-HTTP at `/mcp/` — name is historical)* running on port 6341
- For stdio transport: `pip install -e .` so `palinode-mcp` is on PATH

---

## Claude Code

### Option 1: CLI (quickest)

```bash
# HTTP (remote)
claude mcp add palinode --transport http --url http://your-server:6341/mcp/

# stdio (local)
claude mcp add palinode -- palinode-mcp
```

### Option 2: Project config (`.mcp.json` in project root)

```json
{
  "mcpServers": {
    "palinode": {
      "type": "http",
      "url": "http://your-server:6341/mcp/"
    }
  }
}
```

> **URL note:** Always use the trailing slash (`/mcp/` not `/mcp`). Without it, the server issues a 307 redirect that strict MCP clients may reject.

### Option 3: Global config (`~/.claude/settings.json`)

```json
{
  "mcpServers": {
    "palinode": {
      "command": "palinode-mcp",
      "env": {
        "PALINODE_API_HOST": "your-server",
        "PALINODE_API_PORT": "6340"
      }
    }
  }
}
```

Restart Claude Code after editing config files. See [INSTALL-CLAUDE-CODE.md](INSTALL-CLAUDE-CODE.md) for the full guide including LaunchAgent setup and the session skill.

---

## Claude Desktop

Edit `claude_desktop_config.json`:
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

**HTTP (remote, streamable-HTTP transport):**
```json
{
  "mcpServers": {
    "palinode": {
      "type": "http",
      "url": "http://your-server:6341/mcp/"
    }
  }
}
```

> `palinode-mcp-sse` serves **streamable-HTTP** at `/mcp/` — the binary name is historical. Configure clients with `"type": "http"` (not `"type": "sse"`). Always include the trailing slash in the URL.

**stdio (local):**
```json
{
  "mcpServers": {
    "palinode": {
      "command": "palinode-mcp",
      "env": {
        "PALINODE_DIR": "/path/to/.palinode"
      }
    }
  }
}
```

Restart Claude Desktop after saving.

---

## Cursor, Windsurf, Continue, Cline, Zed

See **[MCP-INSTALL-RECIPES.md](MCP-INSTALL-RECIPES.md)** for complete per-client workflows
including exact config snippets, restart sequences, and troubleshooting blocks.

---

## Codex CLI

Add to `~/.codex/config.toml`:

```toml
[mcp_servers.palinode]
url = "http://your-server:6341/mcp/"
```

Codex CLI has no skills system. The full palinode MCP tool set is available in conversations once the server is reachable.

---

## Remote setup via SSH (stdio fallback)

If your IDE only supports stdio and you need remote access, pipe over SSH:

```json
{
  "mcpServers": {
    "palinode": {
      "command": "ssh",
      "args": [
        "-o", "StrictHostKeyChecking=no",
        "-o", "BatchMode=yes",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
        "-o", "TCPKeepAlive=yes",
        "youruser@your-server",
        "PALINODE_DIR=~/.palinode palinode-mcp"
      ]
    }
  }
}
```

Requires passwordless SSH (`ssh-copy-id youruser@your-server`). HTTP transport is preferred when available.

The three `ServerAlive*` / `TCPKeepAlive` options keep the SSH session alive across NAT/relay idle timeouts (especially common when piping through a VPN like Tailscale). Without them, the MCP connection silently dies after a few minutes of inactivity and you'll see `Connection reset by peer` in the IDE's MCP logs. The keepalives also let SSH detect a dead connection within ~90s after laptop sleep / WiFi change, so the IDE's reconnect logic kicks in faster.

---

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `PALINODE_DIR` | `~/.palinode` | Memory file directory |
| `PALINODE_API_HOST` | `127.0.0.1` | API server host (MCP connects here) |
| `PALINODE_API_PORT` | `6340` | API server port |
| `PALINODE_MCP_SSE_HOST` | `0.0.0.0` | Bind address for HTTP MCP server |
| `PALINODE_MCP_SSE_PORT` | `6341` | Port for HTTP MCP server |
| `PALINODE_MCP_SURFACE` | `full` | MCP tool advertisement surface: `full` advertises every tool; `core` advertises the hot-path subset while keeping dispatch capability unchanged |
| `PALINODE_PROJECT` | _(auto-detect from CWD)_ | Project context for ambient search |
| `PALINODE_API_BIND_INTENT` | _(unset)_ | Set to `public` to suppress the 0.0.0.0 binding warning for intentional network-exposed deployments (e.g., Tailscale). The warning still fires when this is unset and the API binds to `0.0.0.0`. |

---

## Available tools

| Tool | What it does |
|------|-------------|
| `palinode_session_init` | Session-start context digest: resolved project scope, core memories, recent decisions, open action items |
| `palinode_search` | Semantic + keyword hybrid search over memory |
| `palinode_save` | Write a new memory item (persists to git) |
| `palinode_ingest` | Fetch a URL and save as research reference |
| `palinode_status` | Health check + index stats |
| `palinode_list` | List memory files by category |
| `palinode_read` | Read a specific memory file |
| `palinode_entities` | Entity graph traversal |
| `palinode_history` | Git history of a memory file; `detail="full"` adds per-commit diffs |
| `palinode_timeline` | **Deprecated** — alias for `palinode_history` with `detail="full"`; will be removed in v0.9 |
| `palinode_blame` | Per-line provenance for a memory file |
| `palinode_trace` | Composed provenance lineage for a memory file: sources, saved/changed commits, supersession, typed links, recall |
| `palinode_diff` | What changed across memory in the last N days |
| `palinode_rollback` | Revert a memory file to a previous version |
| `palinode_push` | Push memory changes to remote git |
| `palinode_lint` | Scan for orphaned files, contradictions, stale content |
| `palinode_review` | Advisory project-memory review: composes health signals, proposes corrective ops (read-only) |
| `palinode_trigger` | Register prospective memory triggers |
| `palinode_prompt` | Manage versioned LLM prompt files |
| `palinode_consolidate` | Run memory consolidation |
| `palinode_archive_expired` | Archive ephemeral memories whose TTL has expired |
| `palinode_session_end` | Capture session outcomes to daily notes |
| `palinode_dedup_suggest` | Pre-write check: existing files semantically near a draft (Obsidian wiki contract) |
| `palinode_orphan_repair` | Given a broken `[[wikilink]]`, return semantically near candidate targets |
| `palinode_cluster_neighbors` | Given a file path, return semantically related files NOT already wikilinked to or from it |
| `palinode_topic_coverage` | Given a topic phrase, check whether any wiki page already covers it |
| `palinode_doctor` | Fast diagnostic pass — checks paths, services, config, and index health |
| `palinode_doctor_deep` | Full diagnostic with canary write test (~10–15s) |
| `palinode_depends` | Dependency tree for a milestone/task slug; `unblocked=true` lists ready-to-start items |

### Obsidian wiki-maintenance tools

The four tools `palinode_dedup_suggest`, `palinode_orphan_repair`, `palinode_cluster_neighbors`, and `palinode_topic_coverage` are part of the Obsidian integration's wiki-maintenance contract. They give the LLM cheap embedding-based checks: "does a near-duplicate already exist?", "this `[[wikilink]]` has no target — what's the right file to point at?", "what related pages have no wikilink yet?", and "is this topic already covered?". All four are MCP-callable from any compatible client and are documented in detail in [OBSIDIAN.md](OBSIDIAN.md#the-embedding-tools), including default similarity thresholds and the embedding-text preprocessing that runs before comparison.

---

## Verify it works

After setup, ask your assistant:

```
Use palinode_status to check memory health
```

You should see file counts, index size, and embedding model info. Then try:

```
Search palinode for "recent project decisions"
```

If the status check fails, verify:
1. `palinode-api` is running and reachable (`curl http://your-server:6340/status`)
2. For HTTP transport: `palinode-mcp-sse` *(serves streamable-HTTP at `/mcp/`)* is running (`curl http://your-server:6341/mcp/`)
3. For stdio: `palinode-mcp` is on PATH (`which palinode-mcp`)
4. `PALINODE_DIR` exists and contains at least one `.md` file

If the MCP tools appear but your changes aren't taking effect after a config edit, you may have edited the wrong config file. Each client reads a different path:

```bash
palinode mcp-config --diagnose
```

See [MCP-CONFIG-HOMES.md](MCP-CONFIG-HOMES.md) for the full canonical-location reference.
