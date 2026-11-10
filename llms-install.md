# Installing Palinode (agent bootstrap)

> **This file is for coding agents** (Cline, Cursor, Claude Code, Continue, Zed, …)
> installing Palinode autonomously. It is a top-to-bottom runnable recipe with
> prerequisite **detection** built in — run each block, branch on its output.
> Humans should start with the canonical [Quickstart](docs/QUICKSTART.md); this
> file adapts that contract for an autonomous installer.

Palinode is inspectable, correctable project memory for coding agents. It keeps
decisions and their reasons in a private Markdown store under Git, exposes them
through MCP, and lets the user inspect, trace and correct them. Automatic
capture and recall are opt-in per client (`palinode init --hook` for Claude
Code); this recipe does not enable them. It needs **Python ≥ 3.11** and **git**. The released hybrid path
needs a reachable embedding endpoint such as Ollama with `bge-m3`; v0.21.0 and
later can use explicit lexical retrieval instead. Everything can run on
`localhost`; no cloud account is required. Keep the private memory store separate
from the code checkout.

The two failure modes this recipe is designed to prevent:

1. **Assuming hybrid-mode Ollama is already set up** — it often isn't. Step 2 detects and installs it.
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

## Step 1 — Choose retrieval mode

The clone in Step 0 is hybrid by default. On v0.21.0 or later, set
`PALINODE_RETRIEVAL_MODE=lexical` on both the API/watcher and every CLI/client
process; skip the Ollama work in Steps 2–3. Lexical retrieval is exact-term
retrieval, not a replacement for semantic search or a silent fallback when
hybrid's embedding endpoint is unavailable.

## Step 2 — Ensure Ollama is running for hybrid retrieval (detect, then branch)

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

## Step 3 — Ensure the `bge-m3` embedding model is present (detect, then branch)

```bash
# Detection: OK means bge-m3 is already pulled.
curl -s http://localhost:11434/api/tags \
  | python3 -c "import json,sys; ms=json.load(sys.stdin).get('models',[]); print('OK' if any('bge-m3' in m['name'] for m in ms) else 'MISSING')"
```

If that printed **MISSING**, pull it (≈1.2 GB, one-time):

```bash
ollama pull bge-m3
```

## Step 4 — Create the private memory directory

Your memory is a **private** folder of markdown files, kept **separate** from the
code repo (the repo contains zero memory files). `git init` it so every change is
versioned.

```bash
mkdir -p ~/.palinode/{people,projects,decisions,insights,daily}
cd ~/.palinode && git init -q && cd -
cp palinode.config.yaml.example ~/.palinode/palinode.config.yaml
```

## Step 5 — Start the services (absolute venv paths)

For one foreground local-service path, use the canonical command below in
**terminal one**. Note the absolute `$VENV/bin/` prefix — do not rely on
`PATH`. It stays in the foreground, so leave it running and open terminal two
for Step 6.

```bash
# Set PALINODE_RETRIEVAL_MODE=lexical here only after verifying v0.21.0 or later.
PALINODE_DIR=~/.palinode "$VENV/bin/palinode" start
```

**ALTERNATIVE — do not run this alongside `palinode start`:** for separately
managed API and watcher processes, run these in the background or in separate
terminals instead of the foreground command above:

```bash
# Add PALINODE_RETRIEVAL_MODE=lexical here only after verifying v0.21.0 or later.
PALINODE_DIR=~/.palinode "$VENV/bin/palinode-api" &        # REST API on :6340
PALINODE_DIR=~/.palinode "$VENV/bin/palinode-watcher" &    # auto-indexes on save
```

The MCP server itself is launched **by your editor** via the config in Step 6
(stdio transport) — you do not start it by hand for a local install.

## Step 6 — Wire up your editor (MCP config)

In **terminal two**, reproduce the chosen checkout, its venv, the private store,
and retrieval mode before generating config; a new shell has none of terminal
one's variables. Use the same native client generator for hybrid or lexical
retrieval; replace `codex` with your supported client:

```bash
export PALINODE_HOME="/absolute/path/to/palinode"
export VENV="$PALINODE_HOME/venv"
export PALINODE_DIR="$HOME/.palinode"
# For lexical mode on v0.21.0+, export PALINODE_RETRIEVAL_MODE=lexical here too.
"$VENV/bin/palinode" mcp-config --editor codex --stdio
```

The generator emits the selected client's native config fragment with the
installation-resolved executable and `PALINODE_DIR`; merge it at the displayed
destination and restart the client. For a shared/remote server instead of a
local one, use
`palinode mcp-config --http` to print the connection fragment. It does not bind
a port; see [SECURITY.md](SECURITY.md) for the bearer-token + non-loopback bind
gate before exposing the API or MCP service beyond loopback.

Merge only the Palinode entry into existing settings.
Generation is read-only; redirecting output onto an existing config file would
overwrite it. An interactive preview hides credentials; piped output contains
configured credentials needed for connection, so keep it private. Prefer
`PALINODE_API_TOKEN_FILE` to copying a token into stdio settings.

Common config homes per editor are listed by `palinode mcp-config` (no flags),
and documented in [docs/MCP-CONFIG-HOMES.md](docs/MCP-CONFIG-HOMES.md).

## Step 7 — Verify before declaring success

```bash
# 1. Doctor: catches db_path / watcher / stale-index misconfiguration.
PALINODE_DIR=~/.palinode "$VENV/bin/palinode" doctor

# 2. Status over the REST API: should return JSON with index stats.
curl -s http://localhost:6340/status
```

Then, **from inside your editor**, call `palinode_status`, save a harmless
decision, and start a fresh session that calls `palinode_session_init`. A
healthy lexical install reports indexed FTS retrieval without an embedding
service; a healthy hybrid install reports its reachable embedder. See the
[Quickstart](docs/QUICKSTART.md) for the shared `harbor-notes` acceptance path.

Finally, tell the user where to see what was saved: the read-only inspector at
`http://127.0.0.1:6340/ui/` (loopback only; the port follows
`PALINODE_API_PORT`). Corrections are made through the agent's
`palinode_archive` tool or the CLI, not in the browser; see
[docs/UI.md](docs/UI.md).

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `httpcore` / other `ModuleNotFoundError` from a `palinode-*` command | System Python found the script but not the venv deps | Call it by absolute venv path: `"$VENV/bin/palinode-…"` (Step 0) |
| `palinode doctor` reports Ollama unreachable | Hybrid-mode Ollama daemon not running | Re-run Step 2 detection; start the daemon |
| Embedding / search returns errors | Hybrid `bge-m3` not pulled | Re-run Step 3 detection; `ollama pull bge-m3` |
| Editor shows no `palinode` tools | MCP config not picked up | `palinode mcp-config` (no flags) shows which config files it found and whether they have a `palinode` entry |
| Claude Desktop edits get reverted | Desktop rewrites its config on quit and strips `url`-form entries | Edit its config with the app **quit**, and use the **stdio** block from Step 6 |

See [docs/DOCTOR.md](docs/DOCTOR.md) for the full check catalog and `palinode doctor --fix`.
