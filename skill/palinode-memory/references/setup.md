# Palinode Setup Reference

## Quick Install (OpenClaw)

```bash
# 1. Clone and install
git clone https://github.com/Paul-Kyle/palinode.git ~/palinode
cd ~/palinode && pip install -e .

# 2. Memory directory
mkdir -p ~/.palinode
cp palinode.config.yaml.example ~/.palinode/palinode.config.yaml

# 3. Start services
PALINODE_DIR=~/.palinode python -m palinode.api.server &
PALINODE_DIR=~/.palinode python -m palinode.indexer.watcher &

# 4. Install plugin
cp -r ~/palinode/plugin ~/.openclaw/extensions/openclaw-palinode
openclaw gateway restart
```

Full guide: `docs/INSTALL-OPENCLAW.md` in the repo.

## Key Config Options (palinode.config.yaml)

| Key | Default | Notes |
|---|---|---|
| `memory_dir` | `~/.palinode` | Where markdown files live |
| `ollama_url` | `http://localhost:11434` | Embedding server |
| `embedding_model` | `bge-m3` | Must be pulled in Ollama |
| `core_max_chars` | `8000` | Hard limit on core injection |
| `mid_turn_mode` | `none` | Skip core re-injection on turns 2-49 |
| `top_k` | `3` | Search results per query |
| `consolidation.enabled` | `true` | Weekly OLMo compaction |

## Environment Variables

`PALINODE_DIR` overrides `memory_dir` in config. Set in systemd unit file or shell.

## Troubleshooting

**"No such file or directory: .palinode.db"** — Run `python -m palinode.api.server` once to initialize the DB.

**"bge-m3 not found"** — `ollama pull bge-m3`

**Plugin not injecting** — Check `openclaw status` for extension load errors. Ensure no `"kind": "memory"` in manifest (conflicts with Mem0).

**Slow embeddings** — BGE-M3 is 568MB. First run is slow; subsequent runs use content-hash dedup (~90% skipped).
