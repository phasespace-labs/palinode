# Palinode — systemd units

Version-controlled, templated systemd unit files for the three palinode production services. Installable in **user scope** (default, no root) or **system scope** (`--system`, root) — see [User scope vs system scope](#user-scope-vs-system-scope).

**Linux only.** These are systemd units. macOS users need launchd (not yet provided — file a follow-up if you need it).

---

## Services

| Unit | Entry point | Default port |
|------|-------------|-------------|
| `palinode-api` | `uvicorn palinode.api.server:app` | 6340 |
| `palinode-mcp` | `palinode-mcp-sse` *(serves streamable-HTTP at `/mcp/` — name is historical)* | 6341 |
| `palinode-watcher` | `python -m palinode.indexer.watcher` | — |

`palinode-mcp` and `palinode-watcher` declare `Wants=palinode-api.service` so systemd starts them in the right order.

---

## Quickstart

### 1. Set required environment variables

```bash
export PALINODE_HOME=/home/youruser/palinode        # code root + venv/
export PALINODE_DATA_DIR=/home/youruser/palinode-data  # memory markdown files
export OLLAMA_URL=http://localhost:11434             # or remote Ollama
export EMBEDDING_MODEL=bge-m3
# Optional — defaults shown:
# export API_PORT=6340
# export MCP_PORT=6341
```

### 2. Run the installer

```bash
bash deploy/systemd/install.sh
```

This writes the three unit files to `~/.config/systemd/user/` and runs `systemctl --user daemon-reload`. It does **not** start or enable services yet.

### 3. Enable and start

```bash
systemctl --user enable --now palinode-api palinode-mcp palinode-watcher
```

Or pass `--enable` to the installer to do both steps at once:

```bash
bash deploy/systemd/install.sh --enable
```

### 4. Verify

```bash
systemctl --user status palinode-api palinode-mcp palinode-watcher
curl http://localhost:6340/health
```

Expected response from `/health`:

```json
{"status": "ok", ...}
```

---

## All variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PALINODE_HOME` | `$HOME/palinode` | Palinode code root; must contain `venv/bin/` |
| `PALINODE_DATA_DIR` | `$HOME/palinode-data` | Memory directory (`PALINODE_DIR` inside unit files) |
| `OLLAMA_URL` | `http://localhost:11434` | Ollama API base URL for embeddings |
| `EMBEDDING_MODEL` | `bge-m3` | Model name passed to Ollama |
| `API_PORT` | `6340` | Port for `palinode-api` (uvicorn) |
| `MCP_PORT` | `6341` | Port for `palinode-mcp-sse` (streamable-HTTP transport, configure clients with `"type": "http"` and `"url": "http://host:6341/mcp/"`) |
| `WATCHER_UNIT_NAME` | `palinode-watcher` | Installed name of the watcher/indexer unit. Override when an existing deployment named the watcher unit differently (e.g. `palinode-indexer`) so re-running the installer is idempotent against the live unit instead of writing a second, duplicate unit. The watcher's `ExecStart` (`python -m palinode.indexer.watcher`) is unchanged — only the installed `.service` filename. |
| `PALINODE_API_BIND_INTENT` | *(empty)* | Bind-intent for the API's `0.0.0.0` bind. **Empty (default)** → the API starts and only logs the 0.0.0.0 warning; **no token is required** — the right choice for a token-less, network-isolated host (e.g. Tailscale-only). Set to `public` to suppress the warning, **but** the app then *requires* `PALINODE_API_TOKEN` and refuses to start without one, so only set `public` alongside a token. The check is value-based, so empty is treated as "not public". |

All variables must be set or exported before running `install.sh`; the script exports defaults for any that are unset.

> `SYSTEMD_WANTED_BY` is set automatically by `install.sh` from the chosen scope (`default.target` for `--user`, `multi-user.target` for `--system`) and rendered into each unit's `[Install]` section — you do not set it by hand.

---

## User scope vs system scope

| | `--user` (default) | `--system` |
|---|---|---|
| Unit dir | `~/.config/systemd/user/` | `/etc/systemd/system/` |
| Root | not required | **required** (`sudo -E`) |
| `[Install] WantedBy` | `default.target` | `multi-user.target` |
| Managed with | `systemctl --user …` | `systemctl …` |
| Starts at boot | only with `loginctl enable-linger $USER` | yes (no linger needed) |

User scope is right for a single-user dev box. **System scope** is for a dedicated host where the services run as root under `multi-user.target` and must come up at boot without a logged-in session — this is how the production palinode host is deployed (`/opt/palinode` code, `/var/lib/palinode` data).

### Reconciling a system-scope production host

When the live units are system units that were hand-edited before these templates existed, render the tracked templates over them with the host's real values. Pass `sudo -E` so the exported variables survive into the root environment:

```bash
sudo -E PALINODE_HOME=/opt/palinode \
        PALINODE_DATA_DIR=/var/lib/palinode \
        OLLAMA_URL=http://your-ollama-host:11434 \
        EMBEDDING_MODEL=bge-m3 \
        WATCHER_UNIT_NAME=palinode-indexer \
        bash deploy/systemd/install.sh --system --enable
```

Capture the live units first (`systemctl cat palinode-api palinode-mcp palinode-indexer`) so you can diff before/after and confirm the only changes are the intended ones (journald logging, `WantedBy=multi-user.target`, ordering on `network-online.target`).

### Reconciling an existing deployment whose watcher unit is named differently

A deployment that was hand-installed before these templates existed may have its watcher unit named `palinode-indexer` rather than `palinode-watcher`. Point the installer at the live name so it overwrites the existing unit instead of creating a duplicate:

```bash
WATCHER_UNIT_NAME=palinode-indexer bash deploy/systemd/install.sh
```

Without this, `install.sh` would write a fresh `palinode-watcher.service` alongside the running `palinode-indexer.service`, leaving two watcher units.

---

## Idempotency

Re-running `install.sh` overwrites the existing unit files and runs `daemon-reload` again. It is safe to re-run after upgrading palinode or changing any variable.

---

## Troubleshooting

### Check service status

```bash
systemctl --user status palinode-api
systemctl --user status palinode-mcp
systemctl --user status palinode-watcher
```

### View recent logs

```bash
journalctl --user -u palinode-api -n 50
journalctl --user -u palinode-mcp -n 50
journalctl --user -u palinode-watcher -n 50
```

All three services write to journald (`StandardOutput=journal`). No separate log files.

### API not responding

```bash
curl -v http://localhost:6340/health
# expect {"status": "ok", ...}
curl -v http://localhost:6340/status
# expect {"db": "ok", "watcher": ..., ...}
```

### Watcher not indexing

1. Check that `PALINODE_DATA_DIR` exists and contains `.md` files.
2. Check that `OLLAMA_URL` is reachable: `curl ${OLLAMA_URL}/api/tags`.
3. Check the watcher log: `journalctl --user -u palinode-watcher -n 20`.

### lingering / linger mode (for headless servers)

If you need services to start at boot without an interactive session, enable linger:

```bash
loginctl enable-linger "$USER"
```

This allows systemd user services to run even when no user session is active.

---

## Upgrading an existing deploy

1. Pull the latest code: `git -C "$PALINODE_HOME" pull`
2. Re-install dependencies: `"$PALINODE_HOME/venv/bin/pip" install -e "$PALINODE_HOME"`
3. Re-run the installer (idempotent): `bash "$PALINODE_HOME/deploy/systemd/install.sh"`
4. Restart services: `systemctl --user restart palinode-api palinode-mcp palinode-watcher`

---

## Uninstall

```bash
systemctl --user disable --now palinode-api palinode-mcp palinode-watcher
rm ~/.config/systemd/user/palinode-api.service \
   ~/.config/systemd/user/palinode-mcp.service \
   ~/.config/systemd/user/palinode-watcher.service
systemctl --user daemon-reload
```

---

## Notes

- **Remote Ollama:** prefer a stable hostname in `OLLAMA_URL` rather than a raw IP when the embedding service runs on another machine.
- **Existing deployments:** if you already have hand-edited unit files, compare the rendered templates with your current values before replacing them.

---

## Nix flake (NixOS / nix-darwin)

NixOS users can pull palinode directly from the flake without `pip install`.
A `nixosModules.palinode` and `nixosModules.palinode-mcp` are provided.

```nix
# In your NixOS flake inputs:
inputs.palinode.url = "github:phasespace-labs/palinode";

# In your nixosSystem modules list:
outputs = { nixpkgs, palinode, ... }: {
  nixosConfigurations.your-host = nixpkgs.lib.nixosSystem {
    modules = [
      palinode.nixosModules.palinode
      palinode.nixosModules.palinode-mcp
      ({ ... }: {
        services.palinode.enable = true;
        services.palinode.dataDir = "/var/lib/palinode";
        services.palinode.ollamaUrl = "http://your-ollama-host:11434";

        # Optional: also run the MCP HTTP server on port 6341
        services.palinode-mcp.enable = true;
      })
    ];
  };
};
```

### Module options — `services.palinode`

| Option | Default | Description |
|--------|---------|-------------|
| `enable` | `false` | Enable the API + watcher services |
| `user` | `"palinode"` | System user |
| `group` | `"palinode"` | System group |
| `dataDir` | `"/var/lib/palinode"` | Memory directory (`PALINODE_DIR`) |
| `apiHost` | `"127.0.0.1"` | Bind address for the API server |
| `apiPort` | `6340` | API server port |
| `ollamaUrl` | `"http://localhost:11434"` | Ollama base URL |
| `embeddingModel` | `"bge-m3"` | Ollama embedding model name |
| `bindIntent` | `null` | Set to `"public"` to suppress 0.0.0.0 binding warning |
| `openFirewall` | `false` | Open `apiPort` in the NixOS firewall |

### Module options — `services.palinode-mcp`

| Option | Default | Description |
|--------|---------|-------------|
| `enable` | `false` | Enable the MCP HTTP server |
| `port` | `6341` | MCP server port (streamable-HTTP at `/mcp/`) |
| `openFirewall` | `false` | Open `port` in the NixOS firewall |

The MCP module shares `user`, `group`, `dataDir`, and `apiPort` from `services.palinode`.
Enabling `services.palinode-mcp` automatically enables `services.palinode`.

### Dev shell

```bash
nix develop github:phasespace-labs/palinode
# then: pip install -e '.[dev]'
```

> **Note:** The Nix package build requires `sqlite-vec` and `mcp` to be packaged in nixpkgs.
> These are marked with `# TODO` in `flake.nix`. Community contributions welcome — see
> [palinode#38](https://github.com/phasespace-labs/palinode/issues/38).
