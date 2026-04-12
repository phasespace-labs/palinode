# Palinode MCP Setup

Connect Palinode's 17 MCP tools to your AI coding assistant. Palinode runs as a stdio MCP server — point your editor at the Python entry point.

## Local setup (same machine)

If Palinode is installed locally:

```json
{
  "mcpServers": {
    "palinode": {
      "command": "/path/to/palinode/.venv/bin/python",
      "args": ["-m", "palinode.mcp"]
    }
  }
}
```

## Remote setup (SSH)

If Palinode runs on a remote server:

```json
{
  "mcpServers": {
    "palinode": {
      "command": "ssh",
      "args": [
        "-o", "StrictHostKeyChecking=no",
        "-o", "BatchMode=yes",
        "user@your-server",
        "cd /path/to/palinode && .venv/bin/python -m palinode.mcp"
      ]
    }
  }
}
```

Requires passwordless SSH. If not set up: `ssh-copy-id user@your-server`

## Editor-specific config

### Claude Code

Add to `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "palinode": {
      "command": "/path/to/palinode/.venv/bin/python",
      "args": ["-m", "palinode.mcp"]
    }
  }
}
```

Restart Claude Code after editing. Verify with: `Use palinode_status to check memory health`

### Cursor

Add to `.cursor/mcp.json` in your project root (or `~/.cursor/mcp.json` globally):

```json
{
  "mcpServers": {
    "palinode": {
      "command": "/path/to/palinode/.venv/bin/python",
      "args": ["-m", "palinode.mcp"]
    }
  }
}
```

Restart Cursor after editing. The tools appear in Cursor's MCP panel.

### VS Code (Continue / Cline)

**Continue** — add to `~/.continue/config.json`:

```json
{
  "mcpServers": [
    {
      "name": "palinode",
      "command": "/path/to/palinode/.venv/bin/python",
      "args": ["-m", "palinode.mcp"]
    }
  ]
}
```

**Cline** — add to VS Code settings (`settings.json`):

```json
{
  "cline.mcpServers": {
    "palinode": {
      "command": "/path/to/palinode/.venv/bin/python",
      "args": ["-m", "palinode.mcp"]
    }
  }
}
```

### Zed

Add to `~/.config/zed/settings.json`:

```json
{
  "language_models": {
    "mcp": {
      "palinode": {
        "command": "/path/to/palinode/.venv/bin/python",
        "args": ["-m", "palinode.mcp"]
      }
    }
  }
}
```

### Windsurf

Add to `~/.windsurf/mcp.json`:

```json
{
  "mcpServers": {
    "palinode": {
      "command": "/path/to/palinode/.venv/bin/python",
      "args": ["-m", "palinode.mcp"]
    }
  }
}
```

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `PALINODE_DIR` | `~/.palinode` | Memory file directory |
| `PALINODE_API` | `http://localhost:6340` | API server URL |
| `PALINODE_PROJECT` | _(auto-detect from CWD)_ | Project context for ambient search (ADR-008) |

## Available tools

17 tools across search, save, provenance, consolidation, and session management:

| Tool | What it does |
|------|-------------|
| `palinode_search` | Semantic + keyword hybrid search over memory |
| `palinode_save` | Write a new memory item (persists to git) |
| `palinode_ingest` | Fetch a URL and save as research reference |
| `palinode_status` | Health check + index stats |
| `palinode_list` | List memory files by category |
| `palinode_read` | Read a specific memory file |
| `palinode_entities` | Entity graph traversal |
| `palinode_history` | Git history of a memory file with diff stats |
| `palinode_blame` | Per-line provenance for a memory file |
| `palinode_diff` | What changed across memory in the last N days |
| `palinode_rollback` | Revert a memory file to a previous version |
| `palinode_push` | Push memory changes to remote git |
| `palinode_lint` | Scan for orphaned files, contradictions, stale content |
| `palinode_trigger` | Register prospective memory triggers |
| `palinode_prompt` | Manage versioned LLM prompt files |
| `palinode_consolidate` | Run memory consolidation |
| `palinode_session_end` | Capture session outcomes to daily notes |

## Verify

After setup, ask your assistant:

```
Use palinode_status to check memory health
```

You should see file counts, index size, and embedding model info.
