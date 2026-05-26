# Harness Smoke Checklist

Per-harness smoke checklist for validating Palinode MCP connectivity.
Each harness runs the same 5-call sequence for cross-comparison.
Tracked at issue #345 (parent #342).

---

## Canonical 5-call smoke sequence

Every harness runs these calls in order. The expected output patterns
are the same regardless of transport — only the config and restart
steps differ.

| # | Tool call | Args | Expected output |
|---|-----------|------|-----------------|
| 1 | `palinode_status` | (none) | Contains `Palinode Status` or `Files indexed` or `Chunks indexed` |
| 2 | `palinode_search` | `query: "hello"` | Text response (results or `No results found.`) |
| 3 | `palinode_save` | `content: "Smoke test <harness> <date>", type: "Insight", slug: "smoke-<harness>"` | Confirmation text mentioning saved file path; no `Error` or `Save failed` prefix |
| 4 | `palinode_list` | (none) | Text listing includes `smoke-<harness>` |
| 5 | `palinode_read` | `file_path: "insights/smoke-<harness>.md"` | Body contains `Smoke test` |

After completing all 5 calls, record the result:

```bash
palinode mcp-smoke <harness> --record
```

---

## Tier 1 — CI per PR + every release

### Claude Code

- **Tier:** 1
- **Install recipe:** [INSTALL-CLAUDE-CODE.md](INSTALL-CLAUDE-CODE.md)
- **Config path:** `~/.claude.json` (project-scoped) or `.mcp.json` (project-local)

**Smoke sequence:**

1. `palinode_status` — expect `Palinode Status` block with file/chunk counts.
2. `palinode_search` with `query: "hello"` — expect text response.
3. `palinode_save` with `content: "Smoke test claude-code <date>", type: "Insight", slug: "smoke-claude-code"` — expect saved confirmation.
4. `palinode_list` — expect `smoke-claude-code` in output.
5. `palinode_read` with `file_path: "insights/smoke-claude-code.md"` — expect body containing `Smoke test`.

**Troubleshooting:** If status returns `API unreachable`, confirm `palinode-api` is running (`curl http://127.0.0.1:6340/status`).

---

### Codex

- **Tier:** 1
- **Install recipe:** [INSTALL-CLAUDE-CODE.md](INSTALL-CLAUDE-CODE.md) (Codex section)
- **Config path:** `~/.codex/config.toml` (global) or `.codex/config.toml` (project, trusted)

**Smoke sequence:**

1. `palinode_status` — expect `Palinode Status` block with file/chunk counts.
2. `palinode_search` with `query: "hello"` — expect text response.
3. `palinode_save` with `content: "Smoke test codex <date>", type: "Insight", slug: "smoke-codex"` — expect saved confirmation.
4. `palinode_list` — expect `smoke-codex` in output.
5. `palinode_read` with `file_path: "insights/smoke-codex.md"` — expect body containing `Smoke test`.

**Troubleshooting:** If tools do not appear, run `codex mcp` to verify the palinode server is registered in config.toml.

---

### Antigravity

- **Tier:** 1
- **Install recipe:** [MCP-INSTALL-RECIPES.md](MCP-INSTALL-RECIPES.md) (Antigravity section — TBD)
- **Config path:** `~/.gemini/antigravity/mcp_config.json` (cross-platform; see MCP-CONFIG-HOMES.md)

**Smoke sequence:**

1. `palinode_status` — expect `Palinode Status` block with file/chunk counts.
2. `palinode_search` with `query: "hello"` — expect text response.
3. `palinode_save` with `content: "Smoke test antigravity <date>", type: "Insight", slug: "smoke-antigravity"` — expect saved confirmation.
4. `palinode_list` — expect `smoke-antigravity` in output.
5. `palinode_read` with `file_path: "insights/smoke-antigravity.md"` — expect body containing `Smoke test`.

**Troubleshooting:** If tools do not appear, open MCP Servers panel (three-dot menu in chat) and verify palinode is listed and connected.

---

## Tier 2 — Release-blocking manual smoke

Tier 2 includes all Tier 1 harnesses plus the following. Each must pass
before a release is tagged.

### Cursor

- **Tier:** 2
- **Install recipe:** [MCP-INSTALL-RECIPES.md](MCP-INSTALL-RECIPES.md#1-cursor)
- **Config path:** `~/.cursor/mcp.json` (global) or `.cursor/mcp.json` (project)

**Smoke sequence:**

1. `palinode_status` — expect `Palinode Status` block.
2. `palinode_search` with `query: "hello"` — expect text response.
3. `palinode_save` with `content: "Smoke test cursor <date>", type: "Insight", slug: "smoke-cursor"` — expect saved confirmation.
4. `palinode_list` — expect `smoke-cursor` in output.
5. `palinode_read` with `file_path: "insights/smoke-cursor.md"` — expect body containing `Smoke test`.

**Troubleshooting:** If tools do not appear, check Settings > Features > MCP for an error badge; usually a PATH problem.

---

### Claude Desktop

- **Tier:** 2
- **Install recipe:** [MCP-SETUP.md](MCP-SETUP.md) (Claude Desktop section)
- **Config path:** `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS)

**Smoke sequence:**

1. `palinode_status` — expect `Palinode Status` block.
2. `palinode_search` with `query: "hello"` — expect text response.
3. `palinode_save` with `content: "Smoke test claude-desktop <date>", type: "Insight", slug: "smoke-claude-desktop"` — expect saved confirmation.
4. `palinode_list` — expect `smoke-claude-desktop` in output.
5. `palinode_read` with `file_path: "insights/smoke-claude-desktop.md"` — expect body containing `Smoke test`.

**Troubleshooting:** If changes to config are ignored, you edited the wrong file; run `palinode mcp-config --diagnose`.

---

### Cline

- **Tier:** 2
- **Install recipe:** [MCP-INSTALL-RECIPES.md](MCP-INSTALL-RECIPES.md#4-cline--roo-cline-vs-code)
- **Config path:** `~/Library/Application Support/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json` (macOS)

**Smoke sequence:**

1. `palinode_status` — expect `Palinode Status` block.
2. `palinode_search` with `query: "hello"` — expect text response.
3. `palinode_save` with `content: "Smoke test cline <date>", type: "Insight", slug: "smoke-cline"` — expect saved confirmation.
4. `palinode_list` — expect `smoke-cline` in output.
5. `palinode_read` with `file_path: "insights/smoke-cline.md"` — expect body containing `Smoke test`.

**Troubleshooting:** If server shows error badge, `palinode-mcp` is not on PATH; use the absolute path in `"command"`.

---

### Zed

- **Tier:** 2
- **Install recipe:** [MCP-INSTALL-RECIPES.md](MCP-INSTALL-RECIPES.md#5-zed)
- **Config path:** `~/.config/zed/settings.json` (under `context_servers`, not `mcpServers`)

**Smoke sequence:**

1. `palinode_status` — expect `Palinode Status` block.
2. `palinode_search` with `query: "hello"` — expect text response.
3. `palinode_save` with `content: "Smoke test zed <date>", type: "Insight", slug: "smoke-zed"` — expect saved confirmation.
4. `palinode_list` — expect `smoke-zed` in output.
5. `palinode_read` with `file_path: "insights/smoke-zed.md"` — expect body containing `Smoke test`.

**Troubleshooting:** If server not appearing, check for missing `"source": "custom"` in the config entry.

---

### Windsurf

- **Tier:** 2
- **Install recipe:** [MCP-INSTALL-RECIPES.md](MCP-INSTALL-RECIPES.md#2-windsurf)
- **Config path:** `~/.codeium/windsurf/mcp_config.json`

**Smoke sequence:**

1. `palinode_status` — expect `Palinode Status` block.
2. `palinode_search` with `query: "hello"` — expect text response.
3. `palinode_save` with `content: "Smoke test windsurf <date>", type: "Insight", slug: "smoke-windsurf"` — expect saved confirmation.
4. `palinode_list` — expect `smoke-windsurf` in output.
5. `palinode_read` with `file_path: "insights/smoke-windsurf.md"` — expect body containing `Smoke test`.

**Troubleshooting:** If tools not visible, confirm full quit and relaunch; Cascade lazy-loads tools.

---

### Continue

- **Tier:** 2
- **Install recipe:** [MCP-INSTALL-RECIPES.md](MCP-INSTALL-RECIPES.md#3-continue-vs-code)
- **Config path:** `~/.continue/config.yaml`

**Smoke sequence:**

1. `palinode_status` — expect `Palinode Status` block.
2. `palinode_search` with `query: "hello"` — expect text response.
3. `palinode_save` with `content: "Smoke test continue <date>", type: "Insight", slug: "smoke-continue"` — expect saved confirmation.
4. `palinode_list` — expect `smoke-continue` in output.
5. `palinode_read` with `file_path: "insights/smoke-continue.md"` — expect body containing `Smoke test`.

**Troubleshooting:** If tools not visible, ensure you are in Agent mode (not Chat mode).

---

## Tier 3 — Future / best effort

These harnesses are not yet supported for automated smoke testing.
Documentation only; contributions welcome.

### OpenClaw plugin

Not yet integrated into the smoke CLI. The plugin exposes palinode tools
via TypeScript lifecycle hooks, not the MCP stdio/HTTP transport. Smoke
testing requires a different harness approach (tracked in future work).

### Hermes AI

Not yet supported. Config path and MCP integration details TBD.

### Pi

Not yet supported. Config path and MCP integration details TBD.

---

## Related

- [MCP-CONFIG-HOMES.md](MCP-CONFIG-HOMES.md) — canonical config-file locations
- [MCP-INSTALL-RECIPES.md](MCP-INSTALL-RECIPES.md) — copy-paste install workflows
- [VALIDATION-STRATEGY.md](VALIDATION-STRATEGY.md) — four-layer validation model
- `palinode mcp-smoke --list` — list all supported harnesses and tiers
- `palinode mcp-smoke <harness> --record` — record a completed smoke run
