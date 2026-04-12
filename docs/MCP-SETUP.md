# Palinode MCP Setup

Connect Palinode's 17 MCP tools to your AI coding assistant. Two transport options:

| Transport | When to use | Client config |
|-----------|-------------|---------------|
| **Streamable HTTP** | Remote server, any IDE | `"url": "http://your-server:6341/mcp/"` |
| **stdio** | Local install, same machine | `"command": "palinode-mcp"` |

HTTP is recommended for remote setups -- no SSH pipes, survives disconnects, works with every client.

---

## Prerequisites

- Palinode API running (`palinode-api` on port 6340)
- For HTTP transport: `palinode-mcp-sse` running on port 6341
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
      "url": "http://your-server:6341/mcp/"
    }
  }
}
```

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

**HTTP (remote):**
```json
{
  "mcpServers": {
    "palinode": {
      "url": "http://your-server:6341/mcp/"
    }
  }
}
```

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

## Cursor

Add to `.cursor/mcp.json` in your project root, or `~/.cursor/mcp.json` for global config.

**HTTP (remote):**
```json
{
  "mcpServers": {
    "palinode": {
      "url": "http://your-server:6341/mcp/"
    }
  }
}
```

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

Restart Cursor after editing. Tools appear in Settings > MCP.

---

## VS Code (Continue)

Add to `~/.continue/config.json` under the `mcpServers` array.

**HTTP (remote):**
```json
{
  "mcpServers": [
    {
      "name": "palinode",
      "url": "http://your-server:6341/mcp/"
    }
  ]
}
```

**stdio (local):**
```json
{
  "mcpServers": [
    {
      "name": "palinode",
      "command": "palinode-mcp",
      "env": {
        "PALINODE_DIR": "/path/to/.palinode"
      }
    }
  ]
}
```

Reload the Continue extension after editing.

---

## VS Code (Cline)

Add to `cline_mcp_servers.json`:
- macOS: `~/Library/Application Support/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_servers.json`
- Linux: `~/.config/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_servers.json`
- Windows: `%APPDATA%\Code\User\globalStorage\saoudrizwan.claude-dev\settings\cline_mcp_servers.json`

Or open it from VS Code: Cline sidebar > Settings (gear icon) > MCP Servers.

**HTTP (remote):**
```json
{
  "mcpServers": {
    "palinode": {
      "url": "http://your-server:6341/mcp/"
    }
  }
}
```

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

---

## Zed

Add to `~/.config/zed/settings.json` (or open Settings from the command palette).

**HTTP (remote):**
```json
{
  "context_servers": {
    "palinode": {
      "settings": {
        "url": "http://your-server:6341/mcp/"
      }
    }
  }
}
```

**stdio (local):**
```json
{
  "context_servers": {
    "palinode": {
      "settings": {
        "command": {
          "path": "palinode-mcp",
          "env": {
            "PALINODE_DIR": "/path/to/.palinode"
          }
        }
      }
    }
  }
}
```

---

## Windsurf

Add to `~/.codeium/windsurf/mcp_config.json`.

**HTTP (remote):**
```json
{
  "mcpServers": {
    "palinode": {
      "serverUrl": "http://your-server:6341/mcp/"
    }
  }
}
```

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

Restart Windsurf after editing.

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
        "youruser@your-server",
        "PALINODE_DIR=~/.palinode palinode-mcp"
      ]
    }
  }
}
```

Requires passwordless SSH (`ssh-copy-id youruser@your-server`). HTTP transport is preferred when available.

---

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `PALINODE_DIR` | `~/.palinode` | Memory file directory |
| `PALINODE_API_HOST` | `127.0.0.1` | API server host (MCP connects here) |
| `PALINODE_API_PORT` | `6340` | API server port |
| `PALINODE_MCP_SSE_HOST` | `0.0.0.0` | Bind address for HTTP MCP server |
| `PALINODE_MCP_SSE_PORT` | `6341` | Port for HTTP MCP server |
| `PALINODE_PROJECT` | _(auto-detect from CWD)_ | Project context for ambient search |

---

## Available tools (17)

| Tool | What it does |
|------|-------------|
| `palinode_search` | Semantic + keyword hybrid search over memory |
| `palinode_save` | Write a new memory item (persists to git) |
| `palinode_ingest` | Fetch a URL and save as research reference |
| `palinode_status` | Health check + index stats |
| `palinode_list` | List memory files by category |
| `palinode_read` | Read a specific memory file |
| `palinode_entities` | Entity graph traversal |
| `palinode_history` | Git history of a memory file |
| `palinode_blame` | Per-line provenance for a memory file |
| `palinode_diff` | What changed across memory in the last N days |
| `palinode_rollback` | Revert a memory file to a previous version |
| `palinode_push` | Push memory changes to remote git |
| `palinode_lint` | Scan for orphaned files, contradictions, stale content |
| `palinode_trigger` | Register prospective memory triggers |
| `palinode_prompt` | Manage versioned LLM prompt files |
| `palinode_consolidate` | Run memory consolidation |
| `palinode_session_end` | Capture session outcomes to daily notes |

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
2. For HTTP transport: `palinode-mcp-sse` is running (`curl http://your-server:6341/mcp/`)
3. For stdio: `palinode-mcp` is on PATH (`which palinode-mcp`)
4. `PALINODE_DIR` exists and contains at least one `.md` file
