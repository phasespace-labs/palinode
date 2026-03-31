# Palinode — Claude Code Setup

## Add Palinode MCP to Claude Code

Add this to `~/.claude/claude_desktop_config.json` on your Mac or remote server:

```json
{
  "mcpServers": {
    "palinode": {
      "command": "ssh",
      "args": [
        "-o", "StrictHostKeyChecking=no",
        "-o", "BatchMode=yes",
        "user@your-palinode-server",
        "cd ***REMOVED***/clawd/palinode && venv/bin/python -m palinode.mcp"
      ]
    }
  }
}
```

Or by IP (if Tailscale hostname doesn't resolve):
```json
{
  "mcpServers": {
    "palinode": {
      "command": "ssh",
      "args": [
        "-o", "StrictHostKeyChecking=no",
        "user@your-palinode-server",
        "cd ***REMOVED***/clawd/palinode && venv/bin/python -m palinode.mcp"
      ]
    }
  }
}
```

> **Note:** SSH key auth must be set up — passwordless SSH from your Mac to your-server.
> If not set up: `ssh-copy-id user@your-palinode-server`

## Available Tools

| Tool | What it does |
|---|---|
| `palinode_search` | Semantic search over all memory files |
| `palinode_save` | Write a new memory item (persists to git) |
| `palinode_ingest` | Fetch a URL and save as research reference |
| `palinode_status` | Health check + index stats |

## Test it

After adding the config, restart Claude Code and run:
```
Use palinode_status to check memory health
```

Then:
```
Search palinode for "project milestone status"
```

## Relocation (Phase 3)

If Palinode moves to your pgvector server, update the SSH target in claude_desktop_config.json.
The tools API is the same — no client changes needed.
