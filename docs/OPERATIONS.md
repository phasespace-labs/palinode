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

### One-time: mint fact ids on a store that predates them

Consolidation addresses facts by id and harvests only bullets carrying a
`<!-- fact:… -->` marker. Session-end now mints one on every line it appends to
`projects/<project>-status.md`, but it did not before v0.19.1 — so a store that
has been running since before this release has consolidation *targets* full of
bullets the runner cannot address. It skips them, proposes nothing, and reports
`status: success`. Measured on one real store: 449 untagged bullets, 79
consecutive nightly runs, not one proposal.

After upgrading, tag the documents once:

```bash
# What is inert? doctor names the files.
palinode doctor            # consolidation_targets_tagged

# Fix the named document(s) — idempotent, committed with provenance.
palinode bootstrap-ids --file projects/palinode-status.md

# Or tag the whole store (people/, projects/, decisions/, insights/).
palinode bootstrap-ids
```

Skip it and consolidation never runs on those projects, quietly. The cron path
now logs a WARNING naming each skipped project, and the run summary carries
`groups_skipped_untagged` + `skipped_untagged_projects`, so a store still in
this state says so out loud.

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

## Consolidation scheduling

Automatic consolidation runs from one entry point:

```bash
cd /path/to/palinode && PALINODE_DIR=~/palinode venv/bin/python -m palinode.consolidation.cron [--nightly] [--days N]
```

**The crontab is an upper bound on how often the pass may run, not the trigger.**
Before consolidating, the entry point consults the *activity gate*: a pass runs
only when **both** conditions hold since that pass last ran —

| Condition | Config key | Default |
|---|---|---|
| Enough time elapsed | `consolidation.auto_gate.min_hours_elapsed` | 24 |
| Enough sessions recorded | `consolidation.auto_gate.min_sessions` | 5 |

— or when the ceiling `consolidation.auto_gate.max_hours_elapsed` (default 168,
i.e. 7 days) has passed, which runs the pass regardless of session count.

Wall-clock scheduling alone gets both cases wrong: an idle week still burns an
LLM pass over nothing, and a heavy day still waits for the next tick. With the
gate on, schedule the cron as often as hourly and let it decide:

```cron
# Hourly; the gate decides whether anything actually happens.
17 * * * * cd /path/to/palinode && PALINODE_DIR=~/palinode venv/bin/python -m palinode.consolidation.cron --nightly --days 1 >> logs/consolidation.log 2>&1
47 * * * * cd /path/to/palinode && PALINODE_DIR=~/palinode venv/bin/python -m palinode.consolidation.cron --days 3 >> logs/consolidation.log 2>&1
```

A deferred pass exits 0 and logs one line naming both numerators and both
denominators, so the cron log alone answers "why didn't it run last night":

```
2026-09-09 11:00:03 [INFO] palinode.consolidation.cron: Skipping weekly consolidation — deferred: 3 sessions / 5, 11 h / 24 h
```

**Sessions are counted from `daily/`**, as the number of `## Session End —`
entries newer than the last recorded run. `POST /session-end` is the only
writer of that heading and every surface (MCP, CLI, the SessionEnd hook) routes
through it, so the count is exact and needs no separate counter to keep in sync.

**Weekly and nightly are gated independently.** Last-run state lives in
`<memory_dir>/.palinode/consolidation-state.json`, one entry per mode, so
whichever ran last cannot starve the other. A pass that raises records nothing
and is retried on the next tick; a `--dry-run` records nothing either.

Notes:

- **Keep the ceiling.** A store that ingests through the watcher and never
  records a session has no session count to satisfy — without
  `max_hours_elapsed` the dual gate would turn "no sessions" into "never".
- **On-demand runs bypass the gate.** `palinode consolidate`, `palinode dream`,
  `POST /consolidate` and the MCP tool run unconditionally; pass
  `--respect-gate` (`respect_gate: true`) to opt one into the same policy.
- **`--ignore-gate`** forces the cron entry point through, for a hand-run
  recovery.
- **Set `consolidation.auto_gate.enabled: false`** to restore pure wall-clock
  behaviour.
- **Current state is on `/status`** under `consolidation_gate` (`palinode
  status --format json`): the configured thresholds plus, per mode, the last
  run, hours elapsed, sessions since, and whether a pass is due now.

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

### Status documents have rotted

**Symptoms:** a `projects/*-status.md` whose Consolidation Log has grown to hundreds of blank-rationale entries, whose frontmatter counts and dates disagree with its body, or whose `entities:` list carries `<!-- fact:… -->` residue; entity lookups return fragmented refs.

```bash
# Report what would change (dry run — the default)
palinode repair-status

# Repair the status documents, and strip stray fact markers from every other file's frontmatter
palinode repair-status --scope all --execute
```

Nothing is committed — review the diff and commit it yourself. The index is repointed as part of `--execute`; a follow-up `palinode reindex` is not needed. Full option list in [CLI.md](CLI.md#palinode-repair-status).

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

#### From findings to repairs

`palinode lint --propose` turns the deterministic findings into proposed
consolidation operations — detect (lint) → propose (operations with a
rationale) → dispose (the executor). It is a dry run; each proposal names the
finding it came from and whether it is applicable or advisory:

```bash
palinode lint --propose
palinode lint --apply     # implies --propose
```

`--apply` runs the applicable proposals through the same deterministic writers
a consolidation pass uses — the document is retired to `status: archived` with
its `-history.md` audit sibling, the index status is pushed, and the mutation is
one commit. Every trace is stamped with an actor of `lint`, so `git log` and the
history sibling distinguish an operation lint earned from one the model
proposed. Nothing that needs wording is proposed by this path.

The proposal set is also available over the other surfaces:
`POST /lint?propose=true` (add `apply=true` to apply), and the `propose`
argument on the `palinode_lint` MCP tool. `apply` is deliberately CLI- and
API-only: an agent's health scan must not be able to retire memories as a side
effect.

Age-based `ARCHIVE` is restricted by document class (ADR-020) — `people/`,
`projects/`, `decisions/`, identity documents, `core: true`,
`update_policy: replace` and `retirement_policy: superseded-only` documents are
reported as skipped rather than proposed. See
[CLI.md — `palinode lint`](CLI.md#palinode-lint) for the full mapping.

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
| `PALINODE_API_HOST` | `127.0.0.1` | API bind address. Non-loopback requires `PALINODE_API_TOKEN` (or `PALINODE_API_ALLOW_UNAUTH=1` to opt out) — see [SECURITY.md](../SECURITY.md#api-authentication) |
| `PALINODE_CORS_ORIGINS` | `http://localhost:3000,http://127.0.0.1:3000` | Allowed CORS origins (comma-separated) |
| `PALINODE_RATE_LIMIT_SEARCH` | `100` | Max search requests per minute per IP |
| `PALINODE_RATE_LIMIT_WRITE` | `30` | Max write requests per minute per IP |
| `PALINODE_MAX_REQUEST_BYTES` | `5242880` (5MB) | Max request body size |
| `PALINODE_HARNESS` | auto-detected | Harness identity for scoped memory |
| `PALINODE_PROJECT` | auto-detected from CWD | Project context for ambient search boost |
| `PALINODE_MEMBER` | none | Member identity for scoped memory |

---

## Systemd Setup (Linux)

Version-controlled unit-file templates live in [`deploy/systemd/`](../deploy/systemd/). The installer substitutes `${VARIABLE}` placeholders via `envsubst` and is idempotent.

```bash
# Required environment variables
export PALINODE_HOME=/path/to/palinode             # code root + venv/
export PALINODE_DATA_DIR=/path/to/palinode-data    # memory markdown files
export OLLAMA_URL=http://localhost:11434
export EMBEDDING_MODEL=bge-m3
# Optional: API_PORT (default 6340), MCP_PORT (default 6341)

# Install + enable the three user units (palinode-api, palinode-mcp, palinode-watcher)
bash deploy/systemd/install.sh --enable

# Check status
systemctl --user status palinode-api palinode-mcp palinode-watcher
```

For headless servers, enable linger so services start at boot without an active session:

```bash
loginctl enable-linger "$USER"
```

Full reference (variables, idempotency, upgrade, uninstall, troubleshooting): [`deploy/systemd/README.md`](../deploy/systemd/README.md).
