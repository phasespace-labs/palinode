# palinode v0.8.0 — Obsidian, Doctor, Reliable Save

**Release date:** 2026-04-27
**Previous:** v0.7.2 (2026-04-26)

This is a major feature release focused on Obsidian integration, diagnostics, and more reliable save-to-search behavior. It also expands MCP client diagnostics and adds version-controlled systemd templates for self-hosted deployments.

> **Heads up for upgraders:** `POST /save` now includes `indexed: true|false` and `embedded: true|false`, and the response does not return until the new content is searchable.

## Headline features

### Obsidian integration

Palinode can now be initialized as an Obsidian-friendly vault with a clear wiki contract.

- `palinode init --obsidian` scaffolds an opinionated vault layout and starter files.
- `palinode_save` can append an idempotent `## See also` footer when `entities:` are provided.
- `palinode obsidian-sync` applies the same wiki contract to existing files.
- New tools `palinode_dedup_suggest` and `palinode_orphan_repair` help with duplicate detection and broken wikilinks.
- Embedding preprocessing strips wikilink decoration and generated footer blocks before similarity checks.

See [OBSIDIAN.md](OBSIDIAN.md) for the integration guide.

### `palinode doctor` — diagnostic suite

`palinode doctor` is a first-class operational tool for common setup and runtime failures.

- Run `palinode doctor`, `palinode doctor --fast`, or `palinode doctor --deep`.
- `--fix` applies a narrow set of safe automated fixes such as creating missing directories or appending the Palinode block to an existing `CLAUDE.md`.
- `--json` is available for scripts and monitoring.
- MCP surfaces include `palinode_doctor` and `palinode_doctor_deep`.

See [DOCTOR.md](DOCTOR.md) for the full check catalog.

### Save-to-search is now reliable

Two important save-path failure modes are fixed:

1. `POST /save` now embeds content before returning, so newly saved content is immediately searchable.
2. The watcher no longer treats matching content hashes as sufficient proof that vector rows exist, so re-saves can recover from missed embeds.

Save responses now include `indexed` and `embedded` so callers can confirm the result directly.

### MCP client diagnostics expanded

`palinode mcp-config --diagnose` now reports Palinode entries across more client config homes, including Claude Code, Claude Desktop, Cline, and Zed.

- Both `mcpServers` and `context_servers` formats are supported.
- `--json` is available for scripting.
- Divergent configs produce a warning and a non-zero exit code.

See [MCP-INSTALL-RECIPES.md](MCP-INSTALL-RECIPES.md) and [MCP-CONFIG-HOMES.md](MCP-CONFIG-HOMES.md) for client setup details.

### Version-controlled systemd templates

Palinode now ships templated user-level systemd units and an installer under `deploy/systemd/`.

```bash
PALINODE_HOME=/opt/palinode \
PALINODE_DATA_DIR=/var/lib/palinode \
OLLAMA_URL=http://localhost:11434 \
  bash deploy/systemd/install.sh --enable
```

## Other improvements

- Search quality improvements include score-gap dedup, daily-note penalty tuning, canonical-question anchoring, and raw cosine exposure in results.
- `palinode_session_end` now performs semantic dedup against recent saves.
- Startup path validation is stricter, especially around `db_path` and `PALINODE_DIR`.
- `palinode doctor --json` now writes only JSON to stdout.
- `/list` returns newest files first.
- `palinode init` scaffolds both `/save` and `/ps`, with `/save` as the canonical command.

## Documentation

- New or expanded docs: [DOCTOR.md](DOCTOR.md), [OBSIDIAN.md](OBSIDIAN.md), [MCP-INSTALL-RECIPES.md](MCP-INSTALL-RECIPES.md), and [MCP-CONFIG-HOMES.md](MCP-CONFIG-HOMES.md)
- README and QUICKSTART now lead with Obsidian and doctor workflows

## Tests

This release was validated with fresh-install and end-to-end testing across the main setup flows, and several real bugs were fixed during that hardening pass.

## Upgrade

```bash
pip install --upgrade palinode
# or for editable installs:
cd palinode && git pull && pip install -e .
```

If you run as a service via the systemd templates, restart after upgrading:

```bash
systemctl --user restart palinode-api palinode-mcp palinode-watcher
```

After upgrade, run `palinode doctor` to confirm the install is healthy.

## Full changelog

See [CHANGELOG.md](CHANGELOG.md) for the complete entry-by-entry list.
