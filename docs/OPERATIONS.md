# Palinode Operations Guide

How to upgrade, recover from crashes, and maintain a healthy Palinode installation.

---

## Core Safety Guarantee

**Your markdown files are the source of truth.** The SQLite database, vector index, and FTS5 keyword index are all derived from files. If anything goes wrong with the database, delete it and reindex. Your memories are safe as long as the files exist.

```
Files (markdown + YAML frontmatter)  ← source of truth, git-versioned
  ↓ derived
Database (.palinode.db)              ← rebuild anytime with `palinode reindex`
```

---

## Upgrading

### Standard upgrade

```bash
# 1. Backup (always, even if you trust git)
cp -r ~/.palinode ~/.palinode-backup-$(date +%Y%m%d)

# 2. Update code
cd /path/to/palinode
git pull
pip install -e .

# 3. Restart services
systemctl --user restart palinode-api palinode-watcher
# Or however you run them (screen, tmux, Docker, etc.)

# 4. Verify
palinode doctor
palinode status

# 5. Reindex to pick up new features
palinode reindex
```

### What reindex does

For each `.md` file in your memory directory:

1. **Parse** — reads frontmatter and splits body into sections
2. **Hash compare** — computes SHA-256 of each section, checks against stored hash
3. **Skip unchanged** — if hash matches, no Ollama call (zero cost)
4. **Re-embed changed** — if hash differs, calls Ollama BGE-M3 for a new embedding
5. **Update entities** — refreshes the entity graph from frontmatter
6. **Rebuild FTS5** — drops and recreates the keyword search index

**Reindex is safe to run on a live system.** Searches continue to work during reindex. The only brief lock is during FTS5 rebuild (milliseconds).

### What uses Ollama

| Operation | Needs Ollama? | Model | When |
|-----------|:---:|-------|------|
| Reindex (unchanged files) | No | — | Hash matches, skipped |
| Reindex (changed files) | Yes | BGE-M3 | Embeds new content |
| Search | Yes | BGE-M3 | Embeds the query |
| Save | Yes | BGE-M3 | Embeds on write |
| Summary generation | Yes | Chat model | Only for `core: true` files missing summaries |
| List, read, diff, blame, rollback | No | — | File/git operations only |

If Ollama is unreachable during reindex, embedding failures are logged and skipped. The file is not indexed until Ollama comes back and you reindex again.

---

## Recovery Scenarios

### Database corrupted or missing

```bash
# Delete the database
rm ~/.palinode/.palinode.db

# Rebuild from files
palinode reindex
```

Your memories are untouched. The database is rebuilt from scratch. This takes a few minutes for large memory stores (one Ollama call per file section).

### Ollama is down

Everything except search and save continues to work:

| Works without Ollama | Needs Ollama |
|---------------------|-------------|
| `palinode list` | `palinode search` |
| `palinode read` | `palinode save` (embedding step) |
| `palinode diff` | `palinode reindex` (embedding step) |
| `palinode blame` | |
| `palinode history` | |
| `palinode rollback` | |
| `palinode push` | |
| `palinode lint` | |

To check Ollama connectivity:
```bash
palinode doctor
```

### API server won't start

Check the basics:
```bash
# Is the port in use?
lsof -i :6340

# Check logs
journalctl --user -u palinode-api --since "5 minutes ago"

# Try running manually to see errors
PALINODE_DIR=~/.palinode palinode-api
```

Common causes:
- Another process on port 6340
- Missing `PALINODE_DIR` environment variable
- Python dependencies changed (run `pip install -e .`)

### FTS5 index corrupted

Symptoms: keyword searches return errors or no results, but vector search works.

```bash
# Rebuild just the keyword index (fast, no Ollama needed)
palinode rebuild-fts
```

### Git history issues

Palinode auto-commits on save. If git gets into a bad state:

```bash
cd ~/.palinode

# Check status
git status

# If there are uncommitted changes
git add -A && git commit -m "manual recovery commit"

# If HEAD is detached
git checkout main
```

### Watcher crashes with "inotify watch limit reached" (Linux)

The file watcher uses inotify to detect changes. Large memory directories can exceed the default Linux limit.

```bash
# Check current limit
cat /proc/sys/fs/inotify/max_user_watches

# Increase (immediate)
sudo sysctl -w fs.inotify.max_user_watches=524288

# Make permanent
echo 'fs.inotify.max_user_watches=524288' | sudo tee -a /etc/sysctl.conf

# Restart watcher
systemctl --user restart palinode-watcher
```

### File accidentally deleted

```bash
cd ~/.palinode

# Find the last commit that had the file
git log --all -- path/to/deleted-file.md

# Restore it
git checkout <commit-hash> -- path/to/deleted-file.md

# Reindex to update the database
palinode reindex
```

### Memory file has wrong content

Every save is a git commit. Use Palinode's built-in tools:

```bash
# See the file's history
palinode history path/to/file.md

# See what changed
palinode blame path/to/file.md

# Revert to a previous version (creates a new commit, safe)
palinode rollback path/to/file.md
```

Or use the MCP tools from your IDE — `palinode_history`, `palinode_blame`, `palinode_rollback` do the same thing.

---

## Maintenance

### Health check

```bash
palinode doctor
```

Reports: API connectivity, Ollama reachability, file count, embedding health.

### Lint

```bash
palinode lint
```

Scans for: orphaned files, stale active files (>90 days), missing frontmatter fields, missing descriptions, core file count.

### Disk usage

The database is typically 1-5% the size of your memory files. For reference:
- 100 memory files → ~2MB database
- 1,000 memory files → ~20MB database

The largest component is the vector index (1024 floats per chunk).

### Log files

- **API operations log:** `{PALINODE_DIR}/logs/operations.jsonl`
- **MCP audit log:** `{PALINODE_DIR}/.audit/mcp-calls.jsonl` (every tool call with timing)

### Backup strategy

Your memory directory is a git repo. The simplest backup:

```bash
# Push to a remote (GitHub, GitLab, private server)
palinode push

# Or manually
cd ~/.palinode && git push origin main
```

For belt-and-suspenders:
```bash
# Periodic filesystem backup
cp -r ~/.palinode /backup/palinode-$(date +%Y%m%d)
```

The `.palinode.db` file does NOT need to be backed up — it's rebuilt from files with `palinode reindex`.

---

## Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `PALINODE_DIR` | `~/.palinode` | Memory directory root |
| `PALINODE_API_HOST` | `127.0.0.1` | API bind address |
| `PALINODE_CORS_ORIGINS` | `http://localhost:3000,http://127.0.0.1:3000` | Allowed CORS origins (comma-separated) |
| `PALINODE_RATE_LIMIT_SEARCH` | `100` | Max search requests per minute per IP |
| `PALINODE_RATE_LIMIT_WRITE` | `30` | Max write requests per minute per IP |
| `PALINODE_MAX_REQUEST_BYTES` | `5242880` (5MB) | Max request body size |
| `PALINODE_HARNESS` | auto-detected | Harness identity for scoped memory |
| `PALINODE_PROJECT` | auto-detected from CWD | Project context for ambient search boost |
| `PALINODE_MEMBER` | none | Member identity for scoped memory |

---

## Systemd Setup (Linux)

Example service files are in the `systemd/` directory of the repo.

```bash
# Copy to user systemd directory
cp systemd/palinode-api.service ~/.config/systemd/user/
cp systemd/palinode-watcher.service ~/.config/systemd/user/

# Edit paths and environment variables
# Then:
systemctl --user daemon-reload
systemctl --user enable palinode-api palinode-watcher
systemctl --user start palinode-api palinode-watcher

# Check status
systemctl --user status palinode-api
systemctl --user status palinode-watcher
```
