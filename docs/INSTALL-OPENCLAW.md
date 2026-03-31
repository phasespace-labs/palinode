# Installing Palinode with OpenClaw

Palinode is designed as an OpenClaw plugin. This is the primary installation path.

## Prerequisites

- OpenClaw installed and running (`openclaw gateway status`)
- Python 3.12+
- Ollama running with `bge-m3` model (for embeddings)

---

## 1. Install Ollama + BGE-M3

```bash
# Install Ollama
curl -fsSL https://ollama.com/install.sh | sh

# Pull the embedding model
ollama pull bge-m3
```

---

## 2. Clone and Install Palinode

```bash
git clone https://github.com/Paul-Kyle/palinode.git ~/palinode
cd ~/palinode
python3 -m venv venv && source venv/bin/activate
pip install -e .
```

---

## 3. Create Your Memory Directory

```bash
mkdir -p ~/.palinode/{people,projects,decisions,insights,research,daily}
cp palinode.config.yaml.example ~/.palinode/palinode.config.yaml
```

Edit `~/.palinode/palinode.config.yaml`:
```yaml
memory_dir: ~/.palinode
ollama_url: http://localhost:11434
embedding_model: bge-m3
```

---

## 4. Start the Services

### Systemd (recommended)

```bash
# Copy service files
sudo cp deploy/palinode-api.service /etc/systemd/system/
sudo cp deploy/palinode-watcher.service /etc/systemd/system/

# Edit paths in service files
sudo nano /etc/systemd/system/palinode-api.service
# Set: PALINODE_DIR=/home/youruser/.palinode

# Enable and start
sudo systemctl enable palinode-api palinode-watcher
sudo systemctl start palinode-api palinode-watcher

# Verify
curl http://localhost:6340/status
```

### Manual (quick test)

```bash
# Terminal 1: API server
PALINODE_DIR=~/.palinode python -m palinode.api.server

# Terminal 2: File watcher
PALINODE_DIR=~/.palinode python -m palinode.indexer.watcher
```

---

## 5. Install the OpenClaw Plugin

```bash
# Find your OpenClaw extension directory
ls ~/.openclaw/extensions/ 2>/dev/null || ls ~/.openclaw-*/extensions/ 2>/dev/null

# Install plugin (adjust profile name as needed)
cp -r ~/palinode/plugin ~/.openclaw/extensions/openclaw-palinode
# OR for a named profile:
cp -r ~/palinode/plugin ~/.openclaw-field/extensions/openclaw-palinode
```

Edit `~/.openclaw/extensions/openclaw-palinode/package.json` and set:
```json
{
  "config": {
    "apiUrl": "http://localhost:6340",
    "memoryDir": "/home/youruser/.palinode"
  }
}
```

Restart OpenClaw:
```bash
openclaw gateway restart
```

---

## 6. Verify

```bash
# Check plugin loaded
openclaw status

# Test memory tools (in chat)
# /tool palinode_status
# /tool palinode_search {"query": "test"}
```

---

## 7. MCP Setup (for Claude Code)

```bash
# Add to Claude Code MCP config (~/.claude/mcp.json)
{
  "mcpServers": {
    "palinode": {
      "command": "python",
      "args": ["-m", "palinode.mcp"],
      "env": {
        "PALINODE_DIR": "/home/youruser/.palinode"
      }
    }
  }
}
```

---

## Troubleshooting

**Plugin not loading:**
- Check `openclaw status` for extension errors
- Ensure plugin dir name is `openclaw-palinode`
- Remove `"kind": "memory"` from `manifest.json` if present (conflicts with Mem0)

**Embeddings failing:**
- `curl http://localhost:11434/api/tags` — is Ollama running?
- `ollama pull bge-m3` — is the model downloaded?

**API not starting:**
- `PALINODE_DIR` must be set and directory must exist
- Check `journalctl --user -u palinode-api.service -f`
