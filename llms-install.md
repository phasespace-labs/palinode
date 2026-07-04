# Installing Palinode (agent bootstrap)

> **This file is for coding agents** (Cline, Cursor, Claude Code, Continue, Zed, …)
> installing Palinode autonomously. It is a top-to-bottom runnable recipe with
> prerequisite **detection** built in — run each block, branch on its output.
> Humans may prefer the narrative [README](README.md); this file trades prose for
> copy-paste determinism.

Palinode is a local-first persistent-memory MCP server. It needs **Python ≥ 3.11**,
**git**, and a running **Ollama** with the `bge-m3` embedding model. Everything
runs on `localhost`; no cloud account is required.

The two failure modes this recipe is designed to prevent:

1. **Assuming Ollama is already set up** — it often isn't. Step 1 detects and installs it.
2. **Wrong-Python entry points** — the `palinode-*` commands live in the venv's
   `bin/`. If you rely on `PATH` you can hit a script from the venv but its deps
   from system Python (e.g. a spurious `httpcore missing`). **Always call the
   commands by their absolute venv path**, as every block below does via `$VENV`.

---

## Step 0 — Clone and create the virtualenv

```bash
git clone https://github.com/phasespace-labs/palinode && cd palinode
python3 -m venv venv && source venv/bin/activate
pip install -e .

# Pin the absolute venv bin path — use it for EVERY palinode command below.
export VENV="$(pwd)/venv"
"$VENV/bin/palinode" --version   # sanity: prints a version, not a traceback
```

## Step 1 — Ensure Ollama is running (detect, then branch)

```bash
# Detection: OK means the Ollama daemon is reachable on the default port.
curl -s http://localhost:11434/api/tags > /dev/null && echo OK || echo MISSING
```

If that printed **MISSING**, install and start Ollama, then re-run the detection:

```bash
# macOS:
brew install ollama && brew services start ollama
# Linux:
curl -fsSL https://ollama.ai/install.sh | sh && (ollama serve >/tmp/ollama.log 2>&1 &)
```

## Step 2 — Ensure the `bge-m3` embedding model is present (detect, then branch)

```bash
# Detection: OK means bge-m3 is already pulled.
curl -s http://localhost:11434/api/tags \
  | python3 -c "import json,sys; ms=json.load(sys.stdin).get('models',[]); print('OK' if any('bge-m3' in m['name'] for m in ms) else 'MISSING')"
```

If that printed **MISSING**, pull it (≈1.2 GB, one-time):

```bash
ollama pull bge-m3
```

## Step 3 — Create the memory directory

Your memory is a **private** folder of markdown files, kept **separate** from the
code repo (the repo contains zero memory files). `git init` it so every change is
versioned.

```bash
mkdir -p ~/.palinode/{people,projects,decisions,insights,daily}
cd ~/.palinode && git init -q && cd -
cp palinode.config.yaml.example ~/.palinode/palinode.config.yaml
```

## Step 4 — Start the services (absolute venv paths)

Run these in the background (or in separate terminals). Note the absolute
`$VENV/bin/` prefix — do not rely on `PATH`.

```bash
PALINODE_DIR=~/.palinode "$VENV/bin/palinode-api" &        # REST API on :6340
PALINODE_DIR=~/.palinode "$VENV/bin/palinode-watcher" &    # auto-indexes on save
```

The MCP server itself is launched **by your editor** via the config in Step 5
(stdio transport) — you do not start it by hand for a local install.

## Step 5 — Wire up your editor (MCP config)

Let Palinode emit the exact config block for you rather than hand-writing it:

```bash
# Local stdio transport — works with every MCP client, including Claude Desktop.
PALINODE_DIR=~/.palinode "$VENV/bin/palinode" mcp-config --stdio
```

The emitted block uses the bare command name:

```json
{ "mcpServers": { "palinode": { "command": "palinode-mcp", "env": {} } } }
```

Before pasting, **replace `"palinode-mcp"` with its absolute venv path** so the
editor launches the right interpreter (the same wrong-Python trap as Step 0):

```json
{ "mcpServers": { "palinode": { "command": "REPLACE_WITH/venv/bin/palinode-mcp", "env": {} } } }
```

Use the value of `$VENV/bin/palinode-mcp` from Step 0 (run `echo "$VENV/bin/palinode-mcp"`).
(For a shared/remote server instead of a local one, use
`palinode mcp-config --http` — but that binds a network port; see
[SECURITY.md](SECURITY.md) for the bearer-token + bind-intent gate before
exposing it beyond loopback.)

Common config homes per editor are listed by `palinode mcp-config` (no flags),
and documented in [docs/MCP-CONFIG-HOMES.md](docs/MCP-CONFIG-HOMES.md).

## Step 6 — Verify before declaring success

```bash
# 1. Doctor: catches db_path / watcher / stale-index misconfiguration.
PALINODE_DIR=~/.palinode "$VENV/bin/palinode" doctor

# 2. Status over the REST API: should return JSON with index stats.
curl -s http://localhost:6340/status
```

Then, **from inside your editor**, call the `palinode_status` MCP tool. A healthy
install returns index/chunk counts and `Ollama (embed): reachable`. If it does,
the bootstrap succeeded.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `httpcore` / other `ModuleNotFoundError` from a `palinode-*` command | System Python found the script but not the venv deps | Call it by absolute venv path: `"$VENV/bin/palinode-…"` (Step 0) |
| `palinode doctor` reports Ollama unreachable | Ollama daemon not running | Re-run Step 1 detection; start the daemon |
| Embedding / search returns errors | `bge-m3` not pulled | Re-run Step 2 detection; `ollama pull bge-m3` |
| Editor shows no `palinode` tools | MCP config not picked up | `palinode mcp-config` (no flags) shows which config files it found and whether they have a `palinode` entry |
| Claude Desktop edits get reverted | Desktop rewrites its config on quit and strips `url`-form entries | Edit its config with the app **quit**, and use the **stdio** block from Step 5 |

See [docs/DOCTOR.md](docs/DOCTOR.md) for the full check catalog and `palinode doctor --fix`.
