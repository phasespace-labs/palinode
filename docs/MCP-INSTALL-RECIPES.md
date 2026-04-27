# MCP Install Recipes — Per-Client Setup Workflows

Copy-pasteable install workflows for connecting Palinode's MCP server to each
supported AI coding assistant. Pick your client and follow the steps; each
recipe is self-contained.

**Already using Claude Code or Claude Desktop?** See
[INSTALL-CLAUDE-CODE.md](INSTALL-CLAUDE-CODE.md) and the Claude Desktop section
of [MCP-SETUP.md](MCP-SETUP.md) — those clients are covered there.

**Not sure which config file your client actually reads?** Run
`palinode mcp-config --diagnose` — it walks every known canonical path and
reports which ones contain a `palinode` entry. See
[MCP-CONFIG-HOMES.md](MCP-CONFIG-HOMES.md) for the full reference.

---

## Transport quick reference

| Transport | Best for | Key field |
|-----------|----------|-----------|
| **stdio** | Palinode installed on the same machine as the IDE | `"command": "palinode-mcp"` |
| **Streamable HTTP** | Palinode on a remote server, or any IDE that supports it | `"url": "http://host:6341/mcp/"` |

For remote HTTP setups, start `palinode-mcp-sse` on the server before
configuring the client. For stdio, `palinode-mcp` must be on PATH
(`which palinode-mcp`).

---

## 1. Cursor

**Docs verified:** [cursor.com/docs/context/mcp](https://cursor.com/docs/context/mcp)

Cursor reads MCP config from two locations — project-level takes precedence
when both exist:

| Scope | Path |
|-------|------|
| Global (all projects) | `~/.cursor/mcp.json` |
| Project (this repo only) | `.cursor/mcp.json` in project root |

Create the file if it does not exist. If it already exists, merge the
`"palinode"` entry into the existing `"mcpServers"` object.

### stdio (local install)

```json
{
  "mcpServers": {
    "palinode": {
      "command": "palinode-mcp",
      "env": {
        "PALINODE_API_HOST": "127.0.0.1",
        "PALINODE_API_PORT": "6340"
      }
    }
  }
}
```

### HTTP (remote server)

```json
{
  "mcpServers": {
    "palinode": {
      "url": "http://your-server:6341/mcp/"
    }
  }
}
```

Replace `your-server` with your server's hostname or IP (for example
`localhost:6341` for a local HTTP server or a stable hostname for remote).

### Restart sequence

1. Save the file.
2. Open Cursor Settings (`Cmd+Shift+J` on macOS / `Ctrl+Shift+J` on
   Windows/Linux) → **Features** → **Model Context Protocol**.
3. If the `palinode` entry shows a red indicator, click the toggle to
   disable then re-enable it. Cursor does not fully hot-reload config
   changes; a **full quit and relaunch** is the safest path when first
   adding an entry.

### Verification

```bash
palinode mcp-config --diagnose
```

Confirm the `palinode` entry appears in the Cursor config path your instance
reads. Then in Cursor chat (Agent mode):

```
Use palinode_status to check memory health
```

### Troubleshooting

| Symptom | Fix |
|---------|-----|
| Tools do not appear in agent tool list | Settings → MCP — check for an error badge. Usually a PATH problem: run `which palinode-mcp` in a terminal to confirm it is on PATH, then set the full path in `"command"` |
| `palinode mcp-config --diagnose` shows no entry for Cursor | You edited the wrong scope file. Check both `~/.cursor/mcp.json` and `.cursor/mcp.json` |
| Entry shows green in Settings but tools return errors | Palinode API is not running. Run `curl http://127.0.0.1:6340/status` |
| Transport mismatch / server not found | HTTP setups: verify `palinode-mcp-sse` is running on the server (`curl http://your-server:6341/mcp/`) |

---

## 2. Windsurf

**Docs verified:** [docs.windsurf.com/windsurf/cascade/mcp](https://docs.windsurf.com/windsurf/cascade/mcp)

Windsurf stores all MCP config in a single global file:

| Platform | Path |
|----------|------|
| macOS / Linux | `~/.codeium/windsurf/mcp_config.json` |
| Windows | `%USERPROFILE%\.codeium\windsurf\mcp_config.json` |

Create the file and its parent directories if they do not exist:

```bash
mkdir -p ~/.codeium/windsurf
touch ~/.codeium/windsurf/mcp_config.json
```

### stdio (local install)

```json
{
  "mcpServers": {
    "palinode": {
      "command": "palinode-mcp",
      "env": {
        "PALINODE_API_HOST": "127.0.0.1",
        "PALINODE_API_PORT": "6340"
      }
    }
  }
}
```

### HTTP (remote server)

Windsurf uses `serverUrl` for the HTTP endpoint (both `serverUrl` and `url`
are accepted; prefer `serverUrl` as that is what the official docs show):

```json
{
  "mcpServers": {
    "palinode": {
      "serverUrl": "http://your-server:6341/mcp/"
    }
  }
}
```

### Restart sequence

Windsurf does not hot-reload `mcp_config.json`. After saving the file:

1. Fully quit Windsurf (`Cmd+Q` / `Alt+F4`).
2. Relaunch Windsurf.
3. Open the Cascade panel — MCP servers initialise on first Cascade
   conversation.

### Verification

```bash
palinode mcp-config --diagnose
```

Then in a Windsurf Cascade conversation:

```
Use palinode_status to check memory health
```

### Troubleshooting

| Symptom | Fix |
|---------|-----|
| No tools visible in Cascade | Confirm full quit and relaunch; Cascade lazy-loads tools on first use |
| `mcp_config.json` not found | Create the file at the exact path above; the directory is not created on first launch |
| Tools visible but all fail | API not running. `curl http://127.0.0.1:6340/status` should return JSON |
| HTTP: cannot connect | Check `serverUrl` spelling — it is `serverUrl`, not `url`. Confirm the remote MCP server is reachable: `curl http://your-server:6341/mcp/` |
| Cascade tool limit hit | Windsurf caps total tools at 100 across all MCP servers. If other servers are registered, the tool list may be truncated |

---

## 3. Continue (VS Code)

**Docs verified:** [docs.continue.dev/customize/deep-dives/mcp](https://docs.continue.dev/customize/deep-dives/mcp)

Continue migrated from `config.json` (deprecated) to `config.yaml` in
2025. The instructions below use the current YAML format. The global config
lives at `~/.continue/config.yaml`; project-scoped config uses the same
filename under `.continue/config.yaml` in the project root.

> **Important:** MCP tools are only available in Continue's **Agent** mode,
> not Chat mode.

### stdio (local install)

Add an `mcpServers` section to `~/.continue/config.yaml`:

```yaml
mcpServers:
  - name: palinode
    type: stdio
    command: palinode-mcp
    env:
      PALINODE_API_HOST: "127.0.0.1"
      PALINODE_API_PORT: "6340"
```

### HTTP (remote server)

```yaml
mcpServers:
  - name: palinode
    type: streamable-http
    url: http://your-server:6341/mcp/
```

### Restart sequence

Continue hot-reloads config changes when the YAML file is saved — no VS Code
restart needed in most cases. If tools do not appear after saving:

1. Open the VS Code Command Palette (`Cmd+Shift+P` / `Ctrl+Shift+P`).
2. Run **Continue: Reload Config**.
3. Switch to Agent mode in the Continue sidebar.

### Verification

In the Continue Agent sidebar, open the tool list. `palinode_search`,
`palinode_save`, and the other 15+ tools should appear. Then run:

```
Use palinode_status to check memory health
```

Also:

```bash
palinode mcp-config --diagnose
```

### Troubleshooting

| Symptom | Fix |
|---------|-----|
| Tools not visible | Ensure you are in **Agent** mode, not Chat mode |
| YAML parse error | Use 2-space indentation; YAML does not accept tabs |
| Config not picked up | Check whether a project-level `.continue/config.yaml` is overriding the global one |
| `command not found: palinode-mcp` | Continue inherits PATH from the VS Code shell. Run `which palinode-mcp` in your project terminal; if it prints nothing, the venv is not activated. Set `command` to the absolute path returned by `which palinode-mcp` after activating the venv |
| HTTP server errors | `type: streamable-http` (not `sse`) — confirm spelling |

---

## 4. Cline (VS Code)

**Docs verified:** [docs.cline.bot/mcp/configuring-mcp-servers](https://docs.cline.bot/mcp/configuring-mcp-servers)

Cline stores MCP server config in a per-extension settings file whose path
varies by platform:

| Platform | Path |
|----------|------|
| macOS | `~/Library/Application Support/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json` |
| Linux | `~/.config/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json` |
| Windows | `%APPDATA%\Code\User\globalStorage\saoudrizwan.claude-dev\settings\cline_mcp_settings.json` |

The easiest way to open the file is from within VS Code:

1. Open the Cline sidebar.
2. Click the **MCP Servers** icon (plug icon) in Cline's top navigation bar.
3. Select the **Installed** tab.
4. Click **Configure MCP Servers** — this opens `cline_mcp_settings.json`
   directly in the editor.

### stdio (local install)

```json
{
  "mcpServers": {
    "palinode": {
      "command": "palinode-mcp",
      "env": {
        "PALINODE_API_HOST": "127.0.0.1",
        "PALINODE_API_PORT": "6340"
      },
      "disabled": false,
      "alwaysAllow": []
    }
  }
}
```

### HTTP (remote server)

Cline uses `"url"` for remote MCP endpoints (SSE/Streamable HTTP):

```json
{
  "mcpServers": {
    "palinode": {
      "url": "http://your-server:6341/mcp/",
      "disabled": false,
      "alwaysAllow": []
    }
  }
}
```

### Restart sequence

Cline picks up config changes without a VS Code restart. After saving
`cline_mcp_settings.json`:

1. Return to the Cline sidebar → MCP Servers tab.
2. The `palinode` entry should appear. If it shows an error badge, click
   **Restart Server**.
3. No full VS Code restart is needed.

### Verification

```bash
palinode mcp-config --diagnose
```

The diagnose command does not currently know Cline's globalStorage path; it
will not report this file. Instead, verify directly in the Cline sidebar —
the server should show as connected (green) and the tool list should expand
to show all palinode tools.

Then in a Cline conversation:

```
Use palinode_status to check memory health
```

### Troubleshooting

| Symptom | Fix |
|---------|-----|
| Server shows error badge immediately | `palinode-mcp` not found on PATH. Set `"command"` to the absolute path: open a VS Code terminal, activate your palinode venv, run `which palinode-mcp` |
| `cline_mcp_settings.json` not found | The file does not exist until you open Cline's MCP settings at least once. Use the UI path above to let Cline create it |
| Tools appear but all fail | Palinode API not running. Run `curl http://127.0.0.1:6340/status` from a terminal |
| HTTP: `url` field ignored | Confirm the key is lowercase `"url"` — Cline does not accept `serverUrl` |
| `alwaysAllow` prompts every tool call | Add the tool names to the `alwaysAllow` array: `["palinode_search", "palinode_save", "palinode_status"]` |

---

## 5. Zed

**Docs verified:** [zed.dev/docs/ai/mcp](https://zed.dev/docs/ai/mcp)

Zed stores MCP config inside the main `settings.json` under a
`"context_servers"` key (not `"mcpServers"` — this differs from every other
client):

| Platform | Path |
|----------|------|
| macOS / Linux | `~/.config/zed/settings.json` |

Open it from Zed: **Zed** menu → **Settings** → **Open Settings**
(`Cmd+,`).

> **Note:** `"source": "custom"` is required for all manually added entries.
> Without it, Zed silently ignores the entry.

### stdio (local install)

```json
{
  "context_servers": {
    "palinode": {
      "source": "custom",
      "command": "palinode-mcp",
      "args": [],
      "env": {
        "PALINODE_API_HOST": "127.0.0.1",
        "PALINODE_API_PORT": "6340"
      }
    }
  }
}
```

### HTTP (remote server)

Zed does not support a direct `"url"` field for Streamable HTTP. Use
`mcp-remote` as a local bridge:

```json
{
  "context_servers": {
    "palinode": {
      "source": "custom",
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://your-server:6341/mcp/"],
      "env": {}
    }
  }
}
```

`mcp-remote` is an npm package (`npm install -g mcp-remote` or let `npx`
fetch it on first run). It bridges Zed's stdio transport to the remote HTTP
endpoint.

### Restart sequence

Zed **hot-reloads context server settings** — no editor restart needed.
After saving `settings.json`, Zed automatically restarts the context server
process within a few seconds. You can verify in Zed's **Assistant** panel:
open it and check that `palinode` appears in the tool list.

### Verification

```bash
palinode mcp-config --diagnose
```

Zed's `settings.json` path is not currently in palinode's diagnose list (it
uses `context_servers`, not `mcpServers`). Verify directly in Zed's
Assistant panel — the server should appear in the available tools list.

Then in a Zed assistant conversation:

```
Use palinode_status to check memory health
```

### Troubleshooting

| Symptom | Fix |
|---------|-----|
| Server not appearing in Assistant panel | Missing `"source": "custom"` — add it; Zed silently skips entries without it |
| `command not found` error in Zed's logs | Zed may not inherit your shell PATH. Set `"command"` to the absolute path of `palinode-mcp` |
| HTTP via mcp-remote hangs | Confirm `npx` is available: `which npx`. If not, install Node.js. Alternatively, `npm install -g mcp-remote` and use `"command": "mcp-remote"` with the URL in `"args"` |
| Tools appear but calls fail | Palinode API not running: `curl http://127.0.0.1:6340/status` |
| Zed prompts for tool permission every call | Set `"agent.always_allow_tool_actions": true` in `settings.json` to auto-approve, or click **Always Allow** in the prompt |

---

## Environment variable reference

All five clients above can pass env vars to the stdio `palinode-mcp` process.
The full variable set:

| Variable | Default | Purpose |
|----------|---------|---------|
| `PALINODE_DIR` | `~/.palinode` | Memory file directory (override if non-default) |
| `PALINODE_API_HOST` | `127.0.0.1` | Host where `palinode-api` listens |
| `PALINODE_API_PORT` | `6340` | Port for `palinode-api` |
| `PALINODE_PROJECT` | _(auto from CWD)_ | Project context for ambient search |

For HTTP transport the env vars are set on the server side, not the client.

---

## Related docs

- [MCP-SETUP.md](MCP-SETUP.md) — transport overview, SSH fallback, and available tools list
- [MCP-CONFIG-HOMES.md](MCP-CONFIG-HOMES.md) — canonical config file paths and `palinode mcp-config --diagnose` reference
- [INSTALL-CLAUDE-CODE.md](INSTALL-CLAUDE-CODE.md) — full Claude Code install guide (LaunchAgent, session skill)
