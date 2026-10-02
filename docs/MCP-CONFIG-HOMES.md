# MCP Config Homes — Canonical Locations Per Client

MCP configuration can live in several places depending on which client you
are configuring and which platform you are on. There is no UI surface in
any running client that tells you which file it is currently reading.
Editing the wrong file silently has no effect — the client keeps using the
old data.

This document lists the canonical locations, explains which client reads
each, and shows how to identify which file your running instance uses.

---

## Generate a client-specific preview

Run the `palinode` command belonging to the installation you want the editor to
launch (for example `"$VENV/bin/palinode"`). The generator uses its installed
script metadata, falling back to its interpreter's scripts directory. It never
selects an unrelated executable from `PATH`. Missing or conflicting candidates
fail with repair instructions; `--executable /absolute/path/to/palinode-mcp`
selects an explicit executable (use its real path, without symlinks or `..`).
Regenerate after moving or removing the installation.

| Selection | Preview command | Destination and merge operation |
|---|---|---|
| Claude Code | `palinode mcp-config --stdio --editor claude-code` | Project-root `.mcp.json`: merge `palinode` into `mcpServers`; approve the project server |
| Codex CLI | `palinode mcp-config --stdio --editor codex` | `~/.codex/config.toml` (or `$CODEX_HOME/config.toml`): merge `[mcp_servers.palinode]`; update an existing table, never duplicate it. Trusted projects can use `.codex/config.toml` |
| Continue | `palinode mcp-config --stdio --editor continue` | `~/.continue/config.yaml`: merge the named item into the `mcpServers` list; retain the existing config name/version/schema/models |
| Claude Desktop | `palinode mcp-config --stdio --editor claude-desktop` | Platform-specific `claude_desktop_config.json` below: merge into `mcpServers` after quitting Desktop |
| Generic | `palinode mcp-config --stdio` | JSON `mcpServers` fragment; consult the client's recipe |

`--client` aliases `--editor`. Unknown selections fail clearly. Codex output is
TOML and Continue output is YAML even when piped; `--json` is rejected for those
clients to prevent pasting the wrong format. For remote MCP, use `--http --url
https://your-server/mcp/` with the selected editor; the Desktop JSON selector
supports stdio only (use Desktop's Connectors UI for remote HTTP).

Generation is a preview: it never writes or merges editor settings. Merge only
the named server; **do not redirect output onto an existing config file**.
Interactive output explains the merge destination. Piped output is only the
native fragment, suitable for a separate private preview file.

Stdio preserves the effective API host/port and memory directory plus explicitly
configured API token/token-file, project, tool-surface and visibility identity
variables. It does not copy unrelated shell secrets or freeze the current working
directory; the client supplies its project directory at launch. For a client-only
checkout on a remote API host, pass an explicit project in the tool call when
needed. Project resolution remains the API/MCP contract.

Interactive previews and diagnostics redact credentials. Raw generation includes
explicitly configured credentials required for a usable connection. Prefer a
`PALINODE_API_TOKEN_FILE` reference for stdio; the generator preserves its absolute
path without reading its contents. `--bearer` remains an explicit HTTP option and
is serialized into the client's supported header field. Keep raw output private.

Schemas checked against [Claude Code MCP documentation](https://code.claude.com/docs/en/mcp),
[official Codex MCP documentation](https://developers.openai.com/codex/mcp/),
and [Continue configuration reference](https://docs.continue.dev/reference).
Automated tests cover formats, merge preservation and process launch; these are
separate from observing an authenticated editor conversation. Other editor recipes
below are documentation guidance, not evidence of an observed client connection.

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

User-scoped servers live in top-level `mcpServers`. Local-scoped entries live
under `projects[absolute-project-path].mcpServers`. Project-scoped servers live
in project-root `.mcp.json`; the generator targets this shareable shape. Prefer
`claude mcp add-json --scope user` when deliberately installing a user-wide entry.
Claude Code resolves conflicting names in local, project, then user scope order.

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


### Claude Desktop 3p variant (macOS only)

```
~/Library/Application Support/Claude-3p/claude_desktop_config.json
```

### Project-local config (Claude Code CLI)

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

### Antigravity (Google)

Antigravity stores MCP config in a JSON file under the `.gemini`
directory. The path is consistent across platforms:

| Platform | Path |
|----------|------|
| macOS / Linux | `~/.gemini/antigravity/mcp_config.json` |
| Windows | `%USERPROFILE%\.gemini\antigravity\mcp_config.json` |

JSON shape: `{ "mcpServers": { "palinode": { ... } } }` — same as
Claude Desktop.

Access from the IDE: three-dot menu in chat > MCP Servers > Manage MCP
Servers > View raw config.

> **Note:** Antigravity is new (2026) and the config path structure may
> evolve. If `~/.gemini/antigravity/mcp_config.json` does not exist,
> check `~/.gemini/settings/mcp_config.json` as a fallback. See
> the internal tracker for updates.

### Other clients

| Client | Config path |
|--------|-------------|
| Cursor | `~/.cursor/mcp.json` (global) or `.cursor/mcp.json` (project) |
| Windsurf | `~/.codeium/windsurf/mcp_config.json` |
| VS Code (Continue) | `~/.continue/config.yaml` |

---

## Transport: stdio vs streamable-HTTP

A `palinode` entry can use one of two transports. The config *file* you edit is
the same (see the tables above) — only the entry shape changes.

### stdio (local install)

The client launches a `palinode-mcp` process per session over stdio:

```json
{
  "mcpServers": {
    "palinode": {
      "command": "palinode-mcp",
      "env": {}
    }
  }
}
```

Use this when palinode is installed on the **same machine** as the client. Each
session pays a Python process cold-start, and a remote server needs an SSH pipe
(see [MCP-SETUP.md](MCP-SETUP.md#remote-setup-via-ssh-stdio-fallback)).

### Streamable-HTTP (remote server — recommended)

The client connects to an already-running `palinode-mcp` HTTP service:

```json
{
  "mcpServers": {
    "palinode": {
      "type": "http",
      "url": "http://<palinode-host>:6341/mcp/"
    }
  }
}
```

Replace `<palinode-host>` with the host running palinode (port `6341`, default
MCP HTTP port). The trailing slash on `/mcp/` is required.

**Why migrate from SSH-stdio to streamable-HTTP:**

- **Warm model.** The HTTP endpoint is a long-running service, so it reuses the
  already-loaded BGE-M3 embedding model — no per-session model warm-up.
- **No Python cold-start.** stdio spawns a fresh `palinode-mcp` interpreter for
  every session; HTTP talks to a process that is already up.
- **No persistent SSH tunnel.** The old remote pattern piped stdio over SSH
  (`ssh … palinode-mcp`); HTTP drops that connection entirely and survives
  client disconnects/reconnects.

> **Auth (forward-compat):** the HTTP endpoint is currently token-less and is
> protected by network isolation (e.g. a private/Tailscale network). When bearer
> auth lands, add it as an `Authorization` header:
>
> ```json
> { "type": "http", "url": "http://<palinode-host>:6341/mcp/",
>   "headers": { "Authorization": "Bearer <token>" } }
> ```
>
> `palinode mcp-config --http --bearer <token>` emits exactly this shape.

> **Claude Desktop note:** Claude Desktop only accepts stdio (`command`+`args`)
> entries — a `"url"`-form entry is silently stripped on quit (see the warning in
> the Claude Desktop section above). Use the **stdio** form for Claude Desktop;
> use **streamable-HTTP** for Claude Code, Cursor, Cline, Zed, and other clients
> that support remote MCP over HTTP.

### Emit a ready-to-paste block

`palinode mcp-config` can print either transport's config block — paste it into
whichever file the diagnostic (below) tells you your client reads:

```bash
# Streamable-HTTP (remote). Substitute your host:
palinode mcp-config --http --host <palinode-host>

# …or pass a full URL:
palinode mcp-config --http --url http://<palinode-host>:6341/mcp/

# stdio (local):
palinode mcp-config --stdio

# stdio pinned to one project, even when the client later opens a linked worktree:
palinode mcp-config --stdio --project harbor-notes

# HTTP scoped to one project (sent as the X-Palinode-Project header):
palinode mcp-config --http --host <palinode-host> --project harbor-notes
```

When piped, the command emits only the raw JSON block (so you can redirect it);
when run interactively it adds guidance and a `claude mcp add` one-liner. It is
read-only — it never writes to any config file.

### Explicit stdio project scope

Use `palinode mcp-config --stdio --project <slug>` when one generated local
MCP entry must consistently use a named project. The generated entry sets
`PALINODE_PROJECT` only for the `palinode-mcp` process started by that client;
two client configs can therefore name different projects without sharing
mutable session state. The resolver reports this as `environment` in
`palinode_session_init` (and the `prime` JSON output's
`project_resolved_by`), while a `project` argument on that tool still wins as
an explicit per-call override.

For a project-local `.mcp.json`, `palinode init --pin-project` writes the same
setting into the block it generates (the slug from `--project`, or the
directory name); without that flag the emitted block is unchanged.

Omit `--project` to retain normal resolution from the client's CWD, including
Git origin/common-directory detection for linked worktrees. The option accepts
only a plain slug, not `project/<slug>` or a path, so generated configuration
cannot turn into a path-like project reference.

### Explicit HTTP project scope

An HTTP client connects to an already-running, possibly shared
`palinode-mcp-http` process on another machine. That process cannot see the
client's working directory or environment, and it never uses its own
directory as the client's project: with nothing from the client, a call is
unscoped (`Scope: none (none)`), unless the operator pinned the server's own
`PALINODE_PROJECT` for every client.

To scope an HTTP client, send its project in the `X-Palinode-Project` header on
every request. `--project` emits it in the client's own header field:

```bash
palinode mcp-config --http --url http://<palinode-host>:6341/mcp/ --project harbor-notes
```

```json
{ "type": "http", "url": "http://<palinode-host>:6341/mcp/",
  "headers": { "X-Palinode-Project": "harbor-notes" } }
```

The header is that client's pinned setting, the HTTP counterpart of a stdio
client's `PALINODE_PROJECT`, and is reported as `environment`. It wins over the
server's own `PALINODE_PROJECT`, and a `project` argument on a tool call still
wins over it. Two client configs with different headers share one server
without sharing scope. The value must be a plain slug (or `project/<slug>`); an
unusable one is refused, never silently replaced.

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
