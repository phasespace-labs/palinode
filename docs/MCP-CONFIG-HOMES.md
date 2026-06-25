# MCP Config Homes — Canonical Locations Per Client

MCP configuration can live in several places depending on which client you
are configuring and which platform you are on. There is no UI surface in
any running client that tells you which file it is currently reading.
Editing the wrong file silently has no effect — the client keeps using the
old data.

This document lists the canonical locations, explains which client reads
each, and shows how to identify which file your running instance uses.

---

## The problem

On macOS with both Claude Desktop and Claude Code CLI installed, at least
three distinct JSON files may each contain an `mcpServers` block:

| Path | Read by |
|------|---------|
| `~/.claude.json` | Claude Code CLI (project-scoped entries) |
| `~/Library/Application Support/Claude/claude_desktop_config.json` | Claude Desktop (the app) |
| `~/Library/Application Support/Claude-3p/claude_desktop_config.json` | Claude Desktop 3p variant |
| `~/.claude/claude_desktop_config.json` | Some third-party integrations |

If you edit `~/.claude.json` believing that is the canonical config and then
relaunch Claude Desktop, nothing changes — because the app reads the
`Library/Application Support` path. There is no error, warning, or log entry
from the client to indicate this.

---

## Canonical locations by client and platform

### Claude Code CLI (all platforms)

```
~/.claude.json
```

The CLI stores project entries under a key matching the project root path.
The `mcpServers` block for a given project is nested inside that entry.
Project-local config in `.mcp.json` at the project root takes precedence
over global entries.

### Claude Desktop

| Platform | Path |
|----------|------|
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |
| Linux | `~/.config/Claude/claude_desktop_config.json` |

This is the file the desktop app reads. The path is not surfaced in the
app's UI. After editing it, quit and relaunch Claude Desktop for changes
to take effect.

> **Warning: Editing `claude_desktop_config.json` — quit Claude Desktop first
> (`cmd+Q` on macOS / `Alt+F4` on Windows). Edits made while the app is running
> are overwritten on the next quit: the app reads the config at launch, holds a
> stripped copy in memory, and writes that copy back to disk when it exits —
> silently destroying any edit you made during the session. Claude Desktop also
> only accepts stdio (`command`+`args`) MCP entries — a `"url"`-form entry is
> silently stripped on quit.**
>
> **Correct recovery order: quit Claude Desktop → edit the file → relaunch.**
>
> One-line repro: add a `"url"` key to a server entry, keep Claude Desktop open,
> edit the file to fix it, then quit — the fixed entry is gone on next open.
> (#373)

### Claude Desktop 3p variant (macOS only)

```
~/Library/Application Support/Claude-3p/claude_desktop_config.json
```

### Project-local config (Claude Code CLI / Cursor / Windsurf)

```
.mcp.json   (in the project root)
```

Palinode's `palinode init` scaffolds this file. A breadcrumb `_warning`
field at the top reminds you to run `palinode mcp-config --diagnose` if
you are not sure which global config your client is also reading.

### Cline (VS Code extension, formerly Claude Dev)

Cline stores MCP servers in VS Code's globalStorage directory.
JSON shape: `{ "mcpServers": { "palinode": { ... } } }` — same as Claude Desktop.

| Platform | Path |
|----------|------|
| macOS | `~/Library/Application Support/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json` |
| Linux | `~/.config/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json` |
| Windows | `%APPDATA%\Code\User\globalStorage\saoudrizwan.claude-dev\settings\cline_mcp_settings.json` |

### Roo Cline (VS Code extension — fork of Cline)

Roo Cline uses a different extension ID (`rooveterinaryinc.roo-cline`) and a different settings filename (`mcp_settings.json`). Same JSON shape as Cline.

| Platform | Path |
|----------|------|
| macOS | `~/Library/Application Support/Code/User/globalStorage/rooveterinaryinc.roo-cline/settings/mcp_settings.json` |
| Linux | `~/.config/Code/User/globalStorage/rooveterinaryinc.roo-cline/settings/mcp_settings.json` |
| Windows | `%APPDATA%\Code\User\globalStorage\rooveterinaryinc.roo-cline\settings\mcp_settings.json` |

### Zed

Zed stores MCP servers under the `context_servers` key in its settings file — **not** `mcpServers`.
JSON shape: `{ "context_servers": { "palinode": { ... } } }`.

| Platform | Path |
|----------|------|
| macOS (primary) | `~/.config/zed/settings.json` |
| macOS (older builds fallback) | `~/Library/Application Support/Zed/settings.json` |
| Linux | `~/.config/zed/settings.json` |

### JetBrains IDEs (AI Assistant)

JetBrains does not use a hand-edited file with a fixed path. MCP servers are
configured via the IDE settings UI:

**Settings → Tools → AI Assistant → Model Context Protocol (MCP)**

The underlying config directory varies by product and version
(`~/Library/Application Support/JetBrains/<Product><Version>/` on macOS,
`~/.config/JetBrains/<Product><Version>/` on Linux). Use the settings panel
rather than editing the directory directly. `palinode mcp-config --diagnose`
does not cover JetBrains for this reason — verify the connection status in
the IDE's MCP settings panel instead.

Available in IntelliJ IDEA, PyCharm, WebStorm, GoLand, Rider, CLion, DataGrip,
and RubyMine. Requires AI Assistant 2025.1+ (bundled in 2025.2).

### Codex CLI (OpenAI)

Codex stores MCP servers in **TOML** format (not JSON) inside its own
config file. Both the CLI and IDE extension share this config.

| Scope | Path |
|-------|------|
| Global (all projects) | `~/.codex/config.toml` |
| Project (trusted projects only) | `.codex/config.toml` in project root |

Each MCP server is a TOML table: `[mcp_servers.palinode]`. Use
`codex mcp` to manage servers from the CLI, or edit `config.toml`
directly.

```toml
[mcp_servers.palinode]
command = "palinode-mcp"

[mcp_servers.palinode.env]
PALINODE_API_HOST = "127.0.0.1"
PALINODE_API_PORT = "6340"
```

### Generic IDE

Some IDEs store MCP config in a JSON file under a hidden config
directory. The path is consistent across platforms:

| Platform | Path |
|----------|------|
| macOS / Linux | `~/.gemini/generic-ide/mcp_config.json` |
| Windows | `%USERPROFILE%\.gemini\generic-ide\mcp_config.json` |

JSON shape: `{ "mcpServers": { "palinode": { ... } } }` — same as
Claude Desktop.

Access from the IDE: three-dot menu in chat > MCP Servers > Manage MCP
Servers > View raw config.

> **Note:** IDE MCP config path structures may
> evolve. If the documented MCP config file does not exist,
> check `~/.gemini/settings/mcp_config.json` as a fallback. See
> issue #345 for updates.

### Other clients

| Client | Config path |
|--------|-------------|
| Cursor | `~/.cursor/mcp.json` (global) or `.cursor/mcp.json` (project) |
| Windsurf | `~/.codeium/windsurf/mcp_config.json` |
| VS Code (Continue) | `~/.continue/config.yaml` |

---

## Quick diagnostic

```bash
palinode mcp-config --diagnose
```

Walks every known canonical path, parses the JSON, and reports which files
have a `palinode` entry. If multiple files have different content, it prints
a `WARNING: configs diverge` block with a side-by-side diff and exits
non-zero.

For scripting or piped output:

```bash
palinode mcp-config --diagnose --json
```

Returns valid JSON with a `diverged` boolean, a `configs` array, and a
`divergences` array.

---

## How to identify which file your running client reads

1. Run `palinode mcp-config --diagnose` — it lists every file found and
   what the palinode entry says in each.
2. Cross-reference with the table above.
3. Edit only the file that matches your client.
4. Restart the client.
5. Run `palinode mcp-config --diagnose` again to confirm the edit landed in
   the file the client will read next time it starts.

---

## Why there is no "right" file

The correct file is whichever one your running client reads — that differs
by client and platform. This tool does not make that judgement for you; it
enumerates the options so you can make the call.

We intentionally do not write to any user config file from `palinode
mcp-config`. User config files are owned by the user and the apps that read
them.

---

## Related

- [MCP-INSTALL-RECIPES.md](MCP-INSTALL-RECIPES.md) — copy-pasteable per-client install workflows (Cursor, Windsurf, Continue, Cline, Zed)
- [MCP-SETUP.md](MCP-SETUP.md) — transport overview, SSH fallback, environment variables, and available tools list
- `palinode mcp-config --diagnose` is the fastest way to confirm which files contain Palinode entries before you edit anything.
