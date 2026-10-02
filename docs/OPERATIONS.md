# Palinode Operations Guide

How to upgrade, recover from crashes, and maintain a healthy Palinode installation.

---

## Core Safety Guarantee

**Your markdown files are the source of truth.** Every memory lives in a file. The vector index and the FTS5 keyword index are derived from those files, so if anything goes wrong with the database you can delete it and reindex: your memories are safe as long as the files exist.

```
Files (markdown + YAML frontmatter)  ← source of truth, git-versioned
  ↓ derived
Database (.palinode.db)              ← rebuild anytime with `palinode reindex`
```

**Two things in the database are not derived from files, and deleting it loses them:**

- **Registered triggers** (`palinode trigger`) — the description, target file, threshold, cooldown, expiry and authority are stored only in the database. Capture them with `palinode trigger list` before deleting, and re-register afterwards.
- **Recall reinforcement** — how often a memory has been retrieved and how recently (`importance`, `last_recalled`, `recall_count`). This only affects result ordering, it decays back to neutral on its own, and losing it costs you nothing but a few weeks of tuning.

Neither holds memory content. Your memories are still safe.

---

## Upgrading

Version-specific steps live with the version. **Upgrading to v0.20 has a
one-pass index migration and a rollback note:
[UPGRADING-v0.20.md](UPGRADING-v0.20.md).**

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

### What uses an embedding or chat model

The default `hybrid` retrieval mode embeds new content and queries. A
v0.21-capable source checkout can use explicit `lexical` mode, which uses the
derived FTS index instead: it needs neither Ollama nor another embedding
endpoint for save/search, but it is exact-term retrieval. Set the same mode on
the API, watcher, CLI, and MCP client processes. `lexical` is not a fallback
for an unreachable hybrid endpoint; choose it only when the installed source
supports it. See the [Quickstart](QUICKSTART.md) for source prerequisites.

| Operation | Needs an embedding endpoint? | Model/service | When |
|-----------|:---:|-------|------|
| Reindex (unchanged files) | No | — | Hash matches, skipped |
| Reindex (changed files) | Hybrid: yes; lexical: no | Any configured embedding endpoint in hybrid | Lexical rebuilds FTS only |
| Search | Hybrid: yes; lexical: no | Any configured embedding endpoint in hybrid | Lexical ranks keyword matches |
| Save | Hybrid: yes; lexical: no | Any configured embedding endpoint in hybrid | Lexical writes FTS without vectors |
| Summary generation | No | Optional chat model | Only for `core: true` files missing summaries |
| Consolidation | No | Optional chat model | Operator-triggered compaction when configured |
| List, read, diff, blame, rollback | No | — | File/git operations only |

If the hybrid embedder is unreachable during reindex, embedding failures are
logged and skipped. The file is not semantically indexed until it returns and
you reindex again. In lexical mode, inspect FTS readiness with `palinode status`
or `palinode doctor`; switching back to hybrid requires a reindex.

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
17 * * * * cd /path/to/palinode && PALINODE_DIR=~/palinode venv/bin/python -m palinode.consolidation.cron --nightly >> logs/consolidation.log 2>&1
47 * * * * cd /path/to/palinode && PALINODE_DIR=~/palinode venv/bin/python -m palinode.consolidation.cron --days 3 >> logs/consolidation.log 2>&1
```

The weekly's `--days 3` matches the shipped default. Keep it that way: the cron
argument silently overrides `consolidation.lookback_days`, so a crontab edited
without the config is the kind of divergence nobody notices until a run behaves
unexpectedly. `palinode doctor` reports the effective lookback and warns when
the two disagree.

The weekly's three days is a *deep clean* over recent notes — the pass that may
ARCHIVE and MERGE, which the nightly deliberately cannot. It is not a safety net
for the nightly: a note the nightly never consolidated is not revisited once it
falls outside the weekly window either. Widening the window is the wrong lever
for that, because the pass pays for context it re-reads.

### The nightly does not have a window

The nightly line above passes no `--days`, because the nightly no longer selects
by calendar window. It keeps a **per-project watermark** — the time of the last
pass that resolved that project — and reads the notes written since. Four
consequences worth knowing before you tune anything:

- **A failed, deferred or lock-refused pass advances nothing**, per project. The
  next run covers the gap by construction, and covers *only* the gap: a project
  that succeeded is not re-read because a different one failed.
- **Timestamps, not dates.** A working day that straddles UTC midnight, or a
  capture appended to an older dated file, is selected exactly once. The old
  date-string cutoff could miss both, and nothing revisited what a window had
  passed over.
- **`--days N` still works and now means the catch-up bound**: how far back a
  cold or long-failed watermark may reach, so one abandoned project cannot hand
  the model months of notes in a single request. The default is
  `consolidation.nightly.lookback_days: 7`, chosen to match
  `auto_gate.max_hours_elapsed` (168 h) — the gate may legitimately let a week
  pass before firing at its ceiling, and a shorter bound would drop notes the
  gate itself chose to wait on. When the bound clamps a mark, the pass logs the
  project, the mark, the bound and how many note files fell in the gap; it is
  never a silent truncation.
- **Each prompt is a fixed size; a busy day takes several.** One prompt carries
  about 6,000 characters of notes, oldest first, and records where it stopped.
  A project with more than that is sent the next prompt, resumed at that point,
  up to `consolidation.nightly.max_prompts_per_project` (default 4) per pass.
  The pass stops early when nothing is pending, or when a prompt fails or is
  cut off at the token cap; the mark then stays at the end of the last prompt
  that resolved. The run result's `prompts_sent` says how many each project
  took, and `notes_pending` what is left for the next pass.

**Upgrading:** nothing to do. An existing cron line keeps working and covers at
least what it used to — a line that was widened to `--days 3` to self-heal
skipped nights is now buying catch-up headroom for the same purpose, and the
nightly's log says so on every run. Dropping `--days` from the nightly line is
the tidier end state, but it is optional. On the first run after the upgrade a
project with no mark starts from the last successful nightly the store recorded
(the `runs` history, or the gate's clock), clamped to the catch-up bound; a
store with no record of a successful pass starts one bound back.

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
whichever ran last cannot starve the other. The recorded time is the pass's
*start*, and the elapsed floor carries one hour of slack, so a daily cron
satisfies the 24 h default no matter how long the previous pass took or how
many seconds the tick drifted; the ceiling has no slack. A pass that raises,
or that finishes `partial` (a project group failed), records nothing and is
retried on the next tick; a `--dry-run` records nothing either.

The same file keeps a short per-mode **outcome history** under `runs` — the
last 30 real passes with their start, status, failed projects and lookback,
including the partial and raised passes the clock ignores. Nothing in the gate
reads it; `palinode doctor`'s `consolidation_last_run` does, to report the
last outcome and count a failure streak (see [DOCTOR.md](DOCTOR.md)).

It also holds the nightly's per-project **watermarks** under `watermarks` —
one timestamp per project, advanced only by a pass that resolved that project.
Deleting the file loses the marks, not the notes: the next nightly cold-starts
from the last recorded successful pass, or one catch-up bound back. Like the
clock and the history, it is derived operational state and never memory
content.

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

## Status log retention

A `projects/<slug>-status.md` fed by `POST /session-end` gains one dated line
per session:

```markdown
- [2026-03-04] Shipped the retrieval receipt. (2 decisions → daily/2026-03-04.md) <!-- fact:proj-status-9c1f02 -->
```

Six months of those is a backlog the weekly compaction cannot digest — an
honest proposal naming each stale line individually runs past any token cap,
so the pass fails and nothing is retired. The weekly pass therefore retires
them itself, before the model is shown anything:

| Config key | Default | Meaning |
|---|---|---|
| `consolidation.status_log_retention_days` | 90 | A dated log line older than this is archived into the `-history.md` sibling. `0` disables the sweep. |

What to know about it operationally:

- **Nothing is deleted.** Every retired line is appended verbatim to
  `projects/<slug>-history.md`, which stays indexed and retrievable on demand
  (`status: archived` keeps it out of default recall).
- **It runs on the weekly pass only**, once per target, before the prompt is
  built. It commits on its own (`palinode age-retention: N status log line(s)
  older than 90d`) and writes one `## Consolidation Log` line naming the range.
- **The run summary reports `age_retired`.** `palinode consolidate --dry-run`
  counts what would be retired and changes nothing.
- **Identity and profile documents are never swept.** A `people/` memory, a
  project's profile document (`projects/<slug>.md`, as distinct from its
  `-status.md`), a `core: true` or `update_policy: replace` document, or
  anything declaring `retirement_policy: superseded-only`, retires only by
  supersession or retraction — never by age (ADR-020). To protect one
  particular status document, add `retirement_policy: superseded-only` to its
  frontmatter.
- **The model can do the same thing in one operation.** `ARCHIVE_BEFORE`
  (weekly `allowed_ops` only) retires every dated log line older than a date it
  names, subject to the same guards.

---

## Recovery Scenarios

### Database corrupted or missing

```bash
# Capture what the rebuild cannot restore (see the note below)
palinode trigger list --format json > ~/triggers-backup.json

# Delete the database
rm ~/.palinode/.palinode.db

# Rebuild from files
palinode reindex
```

Your memories are untouched. The database is rebuilt from scratch. A hybrid
rebuild embeds each changed section; a lexical rebuild recreates only the FTS
index and needs no embedding service.

**Registered triggers and recall statistics do not come back** — they are not
derived from files (see *Core Safety Guarantee*). Re-register triggers from the
capture above; recall statistics restart at neutral and re-accumulate with use.
**If the database is too damaged to read, that capture will fail too** — which
is why the backup section below suggests exporting triggers on a schedule
rather than at the moment you need them.

### Embedding endpoint is down

In hybrid mode, saves still persist their markdown but semantic indexing and
semantic search are unavailable until the endpoint returns. In lexical mode,
save and exact-term search continue without an embedding endpoint.

| Works without an embedding endpoint | Hybrid work that waits for it |
|---------------------|-------------|
| `palinode list`, `read`, `diff`, `blame`, `history`, `rollback`, `push`, `lint` | `palinode search` semantic retrieval |
| Lexical `palinode save`, `search`, and `reindex` | Hybrid save/reindex vector embedding |

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

The `.palinode.db` file is rebuilt from files with `palinode reindex`, so it needs no backup to protect your memories. If you rely on registered triggers, back those up separately — they are not stored in files:

```bash
mkdir -p ~/.palinode-backups
palinode trigger list --format json > ~/.palinode-backups/triggers-$(date +%Y%m%d).json
```

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
| `PALINODE_PROJECT` | auto-detected from CWD | Pins the project for this process: every recall call resolves it ahead of repository/CWD detection. A slug or `project/<slug>` ref; an unusable value is refused (CLI aborts, MCP returns an error, API answers 400). |
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
