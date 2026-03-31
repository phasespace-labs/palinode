# Using Palinode with Hermes Agent

Palinode and Hermes Agent are complementary. Hermes is an agent runtime with a simple memory system. Palinode is memory infrastructure with git versioning, hybrid search, and weekly consolidation. Together: Hermes runs the agent, Palinode stores the memory.

## How They Fit Together

| What | Hermes | Palinode |
|---|---|---|
| Memory format | `MEMORY.md` flat file | Markdown files, git-versioned |
| Recall | FTS5 + LLM summary | BM25 + vector + associative |
| Compaction | Append + manual trim | Operation-based (KEEP/UPDATE/MERGE) |
| History | Session search | Git blame/diff/rollback |
| MCP tools | Via MCP integration | 13 built-in tools |
| Runtime | Agent framework | Memory server |

**Use case:** Run Hermes as your agent. Back it with Palinode for durable, queryable memory that survives across machines and doesn't bloat.

---

## Install Hermes Agent

```bash
# One-line install (NousResearch official)
curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/install.sh | sh

# Or manual
git clone https://github.com/NousResearch/hermes-agent.git ~/.hermes-agent
cd ~/.hermes-agent && pip install -e .
```

Hermes needs an LLM backend. Options:
- **OpenRouter** (easiest): set `OPENROUTER_API_KEY`
- **Local vLLM**: point at your vLLM endpoint
- **Ollama**: configure in `~/.hermes/config.yaml`

---

## Install Palinode

See [INSTALL-OPENCLAW.md](INSTALL-OPENCLAW.md) for full setup, or quickstart:

```bash
git clone https://github.com/Paul-Kyle/palinode.git ~/palinode
cd ~/palinode && pip install -e .
mkdir -p ~/.palinode
PALINODE_DIR=~/.palinode python -m palinode.api.server &
```

---

## Connect Hermes to Palinode via MCP

Hermes supports MCP servers. Add Palinode's MCP server to Hermes' tool config:

```yaml
# ~/.hermes/config.yaml
mcp_servers:
  - name: palinode
    command: python
    args: ["-m", "palinode.mcp"]
    env:
      PALINODE_DIR: ~/.palinode
```

Hermes now has access to all 13 Palinode tools:
- `palinode_search` — find memories by meaning
- `palinode_save` — store typed memories (people, decisions, insights)
- `palinode_trigger` — register prospective recall intentions
- `palinode_diff` — see what changed in memory
- ... and 9 more

---

## Memory Architecture with Both

```
Hermes runtime
    ↓ session start
    ├── MEMORY.md (Hermes native: persona, user prefs)  ← compact, agent identity
    └── Palinode injection (via MCP / plugin hook)        ← rich project/decision memory
         ├── Core files (core:true) — always injected
         ├── Topic search — relevant to current query
         ├── Associative — entity graph expansion
         └── Triggered — prospective recall
    ↓ session end
    └── palinode_save — capture key facts from conversation
```

**Recommended split:**
- Hermes `MEMORY.md` → agent persona, user name/prefs, communication style (2-3K chars max)
- Palinode → everything else: projects, decisions, people, research, insights

---

## Skill Integration

Install the `palinode` OpenClaw skill (if using OpenClaw + Hermes together):

```bash
clawhub install palinode
```

This gives the agent Palinode-awareness with one command.

---

## Why Not Just Use Hermes Memory?

Hermes' `MEMORY.md` is great for bounded identity/preference state. It struggles with:
- Large codebases / multi-project context (too much to fit in MEMORY.md)
- Long-running projects (the file bloats, curation is manual)
- Multi-machine setups (syncing MEMORY.md across machines is ad-hoc)
- Audit trail (no git, no blame, no rollback)
- Search (FTS5 only, no semantic similarity)

Palinode solves all of these. Hermes contributes: multi-backend LLM routing, skills framework, Docker/SSH/Modal execution environments.
