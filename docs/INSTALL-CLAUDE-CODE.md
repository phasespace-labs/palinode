# Installing Palinode with Claude Code

Palinode gives Claude Code persistent memory via MCP — 18 tools for searching, saving, and managing memories across sessions. The `palinode-session` skill auto-captures milestones and decisions during coding, so your memory stays fresh without manual effort.

## Prerequisites

- Claude Code installed (`npm install -g @anthropic-ai/claude-code`)
- Palinode API running (see below)
- Python 3.11+

---

## Quick Decision: Which Option?

| Setup | Best For | Transport | Latency |
|-------|----------|-----------|---------|
| **A. Local** | Palinode + IDE on same machine | stdio | ~5ms |
| **B. Remote SSH** | Homelab server, stdio-only clients | stdio over SSH | ~20ms |
| **C. Remote HTTP** | Homelab server, any IDE (Zed, Cursor, etc.) | Streamable HTTP | ~15ms |
| **D. LaunchAgent** | macOS auto-start for local install | — (service mgmt) | — |

**Recommended for remote setups:** Option C (HTTP). No SSH pipes, works with every MCP client, and survives SSH disconnects.

---

## Option A: Local Install (Same Machine)

Best if you run Claude Code on the same machine as Palinode.

### 1. Install Palinode

```bash
git clone https://github.com/phasespace-labs/palinode.git ~/palinode
cd ~/palinode
python3 -m venv venv && source venv/bin/activate
pip install -e .
```

### 2. Create Memory Directory

```bash
mkdir -p ~/.palinode
cp ~/palinode/palinode.config.yaml.example ~/.palinode/palinode.config.yaml
# Edit ~/.palinode/palinode.config.yaml — set memory_dir: ~/.palinode
```

### 3. Start Palinode Services

```bash
# Terminal 1: API server
PALINODE_DIR=~/.palinode palinode-api
# Runs on localhost:6340

# Terminal 2: File watcher (auto-indexes on save)
PALINODE_DIR=~/.palinode palinode-watcher
```

Keep them running (add to login items, launchd, or systemd as needed).

### 4. Add MCP Server to Claude Code

Edit `~/.claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "palinode": {
      "command": "python",
      "args": ["-m", "palinode.mcp"],
      "env": {
        "PALINODE_DIR": "/Users/youruser/.palinode"
      },
      "cwd": "/Users/youruser/palinode"
    }
  }
}
```

Restart Claude Code. Run `/mcp` to verify `palinode` is connected.

---

## Option B: Remote Server via SSH (stdio)

Best if Palinode runs on a homelab server and your IDE only supports stdio MCP (Claude Code, Claude Desktop).

```json
{
  "mcpServers": {
    "palinode": {
      "command": "ssh",
      "args": [
        "-o", "StrictHostKeyChecking=no",
        "-o", "BatchMode=yes",
        "youruser@your-server",
        "PALINODE_DIR=~/.palinode /path/to/palinode/venv/bin/python -m palinode.mcp"
      ]
    }
  }
}
```

**Prerequisites:**
- SSH key auth set up: `ssh-copy-id youruser@your-server`
- Palinode installed on the server (Option A steps 1-3)
- Palinode API + watcher running on the server

> **Note:** The MCP server is a pure HTTP client — it makes requests to `palinode-api` on localhost. No direct database or filesystem access. This means the API server must be running on the remote host.

---

## Option C: Remote HTTP (Streamable HTTP) ⭐ Recommended for Remote

Best for remote setups. Works with **any** MCP client — no SSH pipes, no local Python install needed. The MCP server runs as a persistent HTTP service on your server.

### Server Setup

On your server, start the Streamable HTTP MCP server:

```bash
PALINODE_DIR=~/.palinode palinode-mcp-sse
# Listens on 0.0.0.0:6341
```

Or use systemd (recommended):

```ini
# ~/.config/systemd/user/palinode-mcp.service
[Unit]
Description=Palinode MCP Server (Streamable HTTP)
After=network.target palinode-api.service

[Service]
Type=simple
WorkingDirectory=/path/to/palinode
Environment="PALINODE_DIR=/path/to/memory-data"
Environment="PALINODE_API_HOST=127.0.0.1"
Environment="PALINODE_API_PORT=6340"
ExecStart=/path/to/palinode/venv/bin/palinode-mcp-sse
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now palinode-mcp.service
```

### Client Configuration

The MCP endpoint is `http://your-server:6341/mcp`. Configure your IDE:

**Claude Code** (`~/.claude/claude_desktop_config.json`):
```json
{
  "mcpServers": {
    "palinode": {
      "url": "http://your-server:6341/mcp/"
    }
  }
}
```

**Claude Desktop** (`claude_desktop_config.json`):
```json
{
  "mcpServers": {
    "palinode": {
      "url": "http://your-server:6341/mcp/"
    }
  }
}
```

**Zed** (Settings → Extensions → MCP → Add Server):
```json
{
  "palinode": {
    "url": "http://your-server:6341/mcp/",
    "headers": {}
  }
}
```

**Cursor** (Settings → MCP → Add Server):
```json
{
  "mcpServers": {
    "palinode": {
      "url": "http://your-server:6341/mcp/"
    }
  }
}
```

**Other IDEs** — any MCP-compatible IDE can connect using the URL `http://your-server:6341/mcp`.

### Network Access

The MCP server needs to be reachable from your IDE. Options:
- **Tailscale** (recommended): Install on both machines, use the Tailscale IP (e.g., `http://100.x.x.x:6341/mcp`)
- **LAN**: Use the server's local IP if on the same network
- **SSH tunnel**: `ssh -L 6341:localhost:6341 youruser@your-server` then use `http://localhost:6341/mcp`

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PALINODE_MCP_SSE_HOST` | `0.0.0.0` | Bind address for MCP HTTP server |
| `PALINODE_MCP_SSE_PORT` | `6341` | Port for MCP HTTP server |
| `PALINODE_API_HOST` | `127.0.0.1` | Where MCP server sends API requests |
| `PALINODE_API_PORT` | `6340` | API server port |

---

## Option D: macOS LaunchAgent (Auto-Start)

Keep Palinode API running in the background on macOS:

```xml
<!-- ~/Library/LaunchAgents/ai.palinode.api.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>ai.palinode.api</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/youruser/palinode/venv/bin/python</string>
        <string>-m</string>
        <string>palinode.api.server</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PALINODE_DIR</key>
        <string>/Users/youruser/.palinode</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
</dict>
</plist>
```

```bash
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/ai.palinode.api.plist
```

---

## Verify It Works

In Claude Code chat:

```
Use palinode_status to check memory health
```

Expected output: file counts, chunk counts, hybrid search status, Ollama reachable.

```
Search palinode for "recent project decisions"
```

---

## Available Tools (17)

| Tool | Description |
|---|---|
| `palinode_search` | Hybrid BM25+vector search over all memories |
| `palinode_save` | Store a typed memory (person, decision, insight, project) |
| `palinode_list` | List memory files with optional type/category filter |
| `palinode_read` | Read the full content of a specific memory file |
| `palinode_ingest` | Fetch a URL and save as research reference |
| `palinode_status` | File counts, index health, entity graph size |
| `palinode_history` | Git history for a file with diff stats and rename tracking |
| `palinode_entities` | List known entities and their relationships |
| `palinode_consolidate` | Run or preview the weekly compaction job |
| `palinode_diff` | See what changed in memory recently (git diff) |
| `palinode_blame` | Who/when each line was written (git blame) |
| `palinode_rollback` | Revert a file to a previous state |
| `palinode_push` | Push memory changes to remote git |
| `palinode_trigger` | Register a prospective recall trigger |
| `palinode_lint` | Run health checks on memory files |
| `palinode_session_end` | Capture session summary, decisions, blockers at end |

---

## Install the Session Skill (Recommended)

The `palinode-session` skill auto-fires during coding — saves milestones, decisions, and progress without you asking. Solves the "Ctrl+C loses everything" problem.

```bash
# Personal (all Claude Code projects)
cp -r /path/to/palinode/skill/palinode-session ~/.claude/skills/

# Or project-level
mkdir -p .claude/skills
cp -r /path/to/palinode/skill/palinode-session .claude/skills/

# Also add the CLAUDE.md template
cp /path/to/palinode/examples/CLAUDE.md .claude/CLAUDE.md
```

The skill auto-fires when:
- Starting a new task → searches for prior context
- Tests pass or feature completes → saves the milestone
- Making a decision → saves with rationale
- ~30 min since last save → saves progress
- Session ending → captures structured summary

---

## Tips for Claude Code Sessions

**Save decisions as you work:**
```
Save to palinode: decided to use SQLite-vec instead of Qdrant because it's simpler to deploy
```

**Check context before starting:**
```
Search palinode for context on this project before we begin
```

**Register triggers for recurring topics:**
```
Register a palinode trigger: when we discuss training data, surface insights/curation-over-volume.md
```

**After a long session:**
```
Save key decisions from this session to palinode
```

---

## Embedding Setup (Required for Vector Search)

Palinode needs an Ollama embedding server with `bge-m3`:

```bash
# Option 1: Local Ollama
ollama pull bge-m3

# Option 2: Remote Ollama (if running on another machine)
# Set in ~/.palinode/palinode.config.yaml:
# ollama_url: http://your-server:11434
```

Without embeddings, Palinode falls back to BM25 keyword search only.

---

## Troubleshooting

**MCP not connecting:**
- Check `~/.claude/claude_desktop_config.json` syntax (valid JSON)
- Verify `PALINODE_DIR` exists and contains at least one `.md` file
- Test manually: `PALINODE_DIR=~/.palinode python -m palinode.mcp`

**"DB not initialized":**
- Run `PALINODE_DIR=~/.palinode python -m palinode.api.server` once to create `.palinode.db`

**SSH option hangs:**
- Test SSH manually: `ssh youruser@your-server echo ok`
- Ensure `BatchMode=yes` and key auth is set up (no password prompts)

**Slow first search:**
- BGE-M3 is 568MB. Cold start takes 10-30s. Subsequent searches are fast (~100ms).
