# Palinode — systemd user units

Version-controlled, templated systemd user-unit files for the three palinode production services.

**Linux only.** These are systemd units. macOS users need launchd (not yet provided — file a follow-up if you need it).

---

## Services

| Unit | Entry point | Default port |
|------|-------------|-------------|
| `palinode-api` | `uvicorn palinode.api.server:app` | 6340 |
| `palinode-mcp` | `palinode-mcp-sse` | 6341 |
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
| `MCP_PORT` | `6341` | Port for `palinode-mcp-sse` |

All variables must be set or exported before running `install.sh`; the script exports defaults for any that are unset.

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
