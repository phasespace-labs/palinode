# Installing Palinode with Claude Code

Palinode gives Claude Code persistent memory via MCP — 13 tools for searching, saving, and managing memories across sessions.

## Prerequisites

- Claude Code installed (`npm install -g @anthropic-ai/claude-code`)
- Palinode API running (see below)
- Python 3.12+

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

### 3. Start Palinode API

```bash
PALINODE_DIR=~/.palinode python -m palinode.api.server
# Runs on localhost:6340
```

Keep it running (add to login items, launchd, or systemd as needed).

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

## Option B: Remote Server via SSH

Best if Palinode runs on a homelab server and Claude Code runs on your laptop.

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
- Palinode installed on the server (Option A steps 1-2)
- Palinode watcher running on the server (for auto-indexing on file changes)

---

## Option C: macOS LaunchAgent (Auto-Start)

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

## Available Tools (13)

| Tool | Description |
|---|---|
| `palinode_search` | Hybrid BM25+vector search over all memories |
| `palinode_save` | Store a typed memory (person, decision, insight, project) |
| `palinode_ingest` | Fetch a URL and save as research reference |
| `palinode_status` | File counts, index health, entity graph size |
| `palinode_history` | File change history with diff stats and rename tracking |
| `palinode_entities` | List known entities and their relationships |
| `palinode_consolidate` | Run or preview the weekly compaction job |
| `palinode_diff` | See what changed in a memory file (git diff) |
| `palinode_blame` | Who/when each section was written |
| `palinode_rollback` | Revert a file to a previous state |
| `palinode_push` | Push memory changes to remote git |
| `palinode_trigger` | Register a prospective recall intention |

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
