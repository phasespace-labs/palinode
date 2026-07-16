# Palinode Deployment Guide

Lessons from running Palinode in production across multiple machines with Claude Code, Claude Desktop, and Cursor.

## Architecture: one server, many clients

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│  Machine A   │     │  Machine B   │     │  Machine C   │
│  Claude Code │     │  Cursor      │     │  Claude Desk │
│              │     │              │     │              │
│  settings:   │     │  settings:   │     │  settings:   │
│  url: host   │     │  url: host   │     │  url: host   │
│  :6341/mcp   │     │  :6341/mcp   │     │  :6341/mcp   │
└──────┬───────┘     └──────┬───────┘     └──────┬───────┘
       │                    │                    │
       └────────────────────┼────────────────────┘
                            │ Streamable HTTP
                   ┌────────▼────────┐
                   │    Server       │
                   │  palinode-api   │  ← port 6340
                   │  palinode-mcp   │  ← port 6341
                   │  palinode-watch │
                   │  ~/.palinode/   │  ← memory files
                   │  .palinode.db   │  ← SQLite-vec
                   └─────────────────┘
```

Install Palinode on **one** machine (the server). Client machines need zero local install — they connect via MCP URL.

## Server setup

```bash
# Install
git clone https://github.com/phasespace-labs/palinode
cd palinode
python3 -m venv venv && source venv/bin/activate
pip install -e .

# Create memory directory
mkdir -p ~/.palinode && cd ~/.palinode && git init
export PALINODE_DIR=~/.palinode

# Configure embeddings (requires Ollama with bge-m3)
cp palinode.config.yaml.example ~/.palinode/palinode.config.yaml
# Edit: set embeddings.primary.url to your Ollama endpoint

# Run services
palinode-api     # FastAPI on :6340
palinode-mcp-http  # MCP Streamable HTTP on :6341
palinode-watcher   # File indexer
```

For production, run the processes as services: Docker Compose (`docker compose up -d` at the repo root — bundles Ollama and the model pull), systemd on Linux (`deploy/systemd/`), or launchd on macOS (`deploy/launchd/`). See the README's **Running as a service** section.

## Client setup

Add to `~/.claude/settings.json` (Claude Code) or `claude_desktop_config.json` (Claude Desktop):

```json
{
  "mcpServers": {
    "palinode": {
      "url": "http://your-server:6341/mcp"
    }
  }
}
```

That's it. No local install needed.

## Transport selection

| Transport | When to use | Config |
|-----------|------------|--------|
| **Streamable HTTP** | Remote server (recommended) | `"url": "http://host:6341/mcp"` |
| **stdio** | Local install, single machine | `"command": "palinode-mcp"` |
| ~~SSH tunnel~~ | Don't — disconnects unpredictably | — |

## SessionEnd hook (auto-capture)

Create `~/.claude/hooks/palinode-session-end.sh` to automatically capture every Claude Code session:

```bash
#!/bin/bash
set -euo pipefail

PALINODE_API="${PALINODE_API_URL:-http://your-server:6340}"

INPUT=$(cat)
TRANSCRIPT_PATH=$(echo "$INPUT" | jq -r '.transcript_path // empty')
CWD=$(echo "$INPUT" | jq -r '.cwd // empty')

if [ -z "$TRANSCRIPT_PATH" ] || [ ! -f "$TRANSCRIPT_PATH" ]; then
  exit 0
fi

# Claude Code transcript format:
#   User:      {type: "user", message: {role: "user", content: "text"}}
#   Assistant: {type: "assistant", message: {content: [{type: "text", text: "..."}]}}
MSG_COUNT=$(jq -r 'select(.type == "user") | .message.content // empty' \
  "$TRANSCRIPT_PATH" 2>/dev/null | grep -c '.' 2>/dev/null || echo "0")

if [ "$MSG_COUNT" -lt 3 ]; then
  exit 0
fi

PROJECT=$(basename "$CWD" 2>/dev/null || echo "unknown")
FIRST_PROMPT=$(jq -r 'select(.type == "user") | .message.content // empty' \
  "$TRANSCRIPT_PATH" 2>/dev/null | head -1 | cut -c1-200)

SUMMARY="Auto-captured session (${MSG_COUNT} messages). Topic: ${FIRST_PROMPT}"

curl -s -o /dev/null \
  -X POST "${PALINODE_API}/session-end" \
  -H "Content-Type: application/json" \
  -d "$(jq -n \
    --arg summary "$SUMMARY" \
    --arg project "$PROJECT" \
    --arg source "claude-code-hook" \
    '{summary: $summary, project: $project, source: $source, decisions: [], blockers: []}'
  )" \
  --connect-timeout 5 \
  --max-time 10 || true
```

Register in `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionEnd": [{
      "hooks": [{
        "type": "command",
        "command": "~/.claude/hooks/palinode-session-end.sh",
        "timeout": 15
      }]
    }]
  }
}
```

**Important:** The transcript format uses nested `message.content`, NOT top-level `content`. Using the wrong format causes silent failures (MSG_COUNT = 0, hook exits early).

## Ambient context search

When searching from a project directory, results from that project are automatically boosted. Configure in `palinode.config.yaml`:

```yaml
context:
  enabled: true
  boost: 1.5
  auto_detect: true       # project/{basename(cwd)} auto-detected
  project_map:             # explicit overrides
    my-project: project/my-project
```

Or set `PALINODE_PROJECT=project/my-project` as an env var.

Use `--no-context` on the CLI to disable: `palinode search "query" --no-context`

## Deployment checklist

- [ ] Server: palinode-api running on :6340
- [ ] Server: palinode-mcp-http running on :6341 (or stdio for local)
- [ ] Server: palinode-watcher running
- [ ] Server: Ollama with bge-m3 reachable
- [ ] Server: git initialized in PALINODE_DIR
- [ ] Client: MCP URL in settings.json
- [ ] Client: SessionEnd hook installed and tested
- [ ] Client: Verify with `palinode status` (CLI) or `palinode_status` (MCP tool)
- [ ] Test: `palinode_save` followed by `palinode_search` returns the saved memory
