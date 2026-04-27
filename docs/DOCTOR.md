# `palinode doctor` — diagnostic guide

`palinode doctor` is a read-only diagnostic command that inspects a running Palinode installation and reports silent-misconfiguration bugs that would otherwise look like data loss. It is the tool you reach for when search returns less than you expected, when "save" feels like it didn't stick, or when something used to work and now does not. It is deliberately scoped: doctor *diagnoses*, it almost never *repairs*. The cases where it does repair are explicitly whitelisted and described below — they never touch user data.

The command exists because Palinode's worst failure mode is *silent success*. Every component can report healthy while serving wrong, stale, or orphan data — for example, after a directory rename in which `palinode.config.yaml` and `PALINODE_DIR` drift apart, the watcher can keep writing to a phantom database for hours while the API serves an unrelated empty one. Doctor is the runtime audit that surfaces those drifts before they become incidents.

Three surfaces share one engine:

- **CLI** — `palinode doctor` (with `--json`, `--check`, `--verbose`, `--fix`, `--yes`, `--dry-run` flags)
- **API** — `GET /doctor?fast=true&canary=false`
- **MCP** — `palinode_doctor` (fast subset, sub-500ms) and `palinode_doctor_deep` (full run with network probes, 10–15s)

The CLI is the canonical surface; the API and MCP tools call into the same `palinode.diagnostics` registry. Read-only by default. `--fix` is CLI-only and applies only to the three whitelisted actions described in [The `--fix` whitelist](#the---fix-whitelist).

---

## Quickstart

```bash
palinode doctor                  # text report with ✓ / ⚠ / ✗ markers
palinode doctor --json           # machine-parseable
palinode doctor --verbose        # also print remediation for passing checks
palinode doctor --check db_path_resolvable   # run one check by name
palinode doctor --fix            # apply whitelisted fixes (per-action y/N prompt)
palinode doctor --fix --dry-run  # show what --fix would do, change nothing
palinode doctor --fix --yes      # CI-friendly: apply fixes without prompting
```

Exit code is `0` when no checks failed and `1` when at least one check failed (regardless of severity). See [Exit codes](#exit-codes).

A typical first response to "memory feels broken" is:

```bash
palinode doctor                  # 1. read the diagnosis
palinode doctor --check phantom_db_files --verbose   # 2. zoom in on suspect check
palinode doctor --fix --dry-run  # 3. preview safe fixes if any apply
```

---

## The check catalog

There are 18 checks across six categories. Severity is one of `info`, `warn`, `error`, `critical`; `passed=True` means the check did not detect a problem (a passed `info` check still appears in the report so the operator can see the resolved state).

### Path integrity

Pure-disk checks. Cheap, no network.

| Check | Severity ceiling | Catches |
|---|---|---|
| `memory_dir_exists` | critical | `PALINODE_DIR` points at a missing or non-directory path |
| `db_path_resolvable` | error | `db_path` parent missing, or the file is not openable as SQLite |
| `db_path_under_memory_dir` | warn | `db_path` resolves outside `memory_dir` (the rename-drift signature) |
| `phantom_db_files` | critical | One or more `.palinode.db` files exist outside the configured path |
| `multiple_palinode_dirs` | warn | `PALINODE_DIR` env var disagrees with the `memory_dir` actually loaded from YAML |

#### `memory_dir_exists`

Verifies `Path(config.memory_dir)` exists and is a directory. Without it nothing else works, so this is the single critical-severity gate. Failure prints the resolved path and the `mkdir -p` command. **Fixable via `--fix`** (creates the directory).

#### `db_path_resolvable`

Verifies the configured `db_path` is openable by SQLite in read-only mode and that its parent directory exists. Uses `PRAGMA schema_version` to validate the SQLite header, which catches a non-SQLite file masquerading as the DB. Full `PRAGMA integrity_check` is left to deeper future checks because it can be slow on large stores.

When the parent directory is missing the remediation tells you to edit `palinode.config.yaml` rather than auto-creating it — a missing parent is almost always a typo or a stale config, not a fresh-install case.

#### `db_path_under_memory_dir`

Resolves both paths and checks that `db_path` is inside `memory_dir`. This is the most common rename-drift signature: the operator updated `PALINODE_DIR` after a directory move but `db_path` in YAML still points at the old location. The remediation tells you to update `palinode.config.yaml`. Doctor does not move the DB (that is data motion; see [The `--fix` whitelist](#the---fix-whitelist)).

#### `phantom_db_files`

The marquee check. Walks a list of plausible roots looking for any `.palinode.db` files that are *not* the configured path:

- `${memory_dir}`
- `${HOME}`, `${HOME}/palinode`, `${HOME}/palinode-data`
- `/var/lib/palinode`
- A handful of historical paths used during development (these are harmless on systems that don't have them)
- Anything listed under `doctor.search_roots` in `palinode.config.yaml`

Files are filtered by SQLite magic bytes (`SQLite format 3\0`) and deduplicated by inode. For each candidate, doctor opens it read-only and reports its size, `chunks` row count, and mtime. If any non-canonical DB is found, severity is **critical** — a stale process may still be writing to it.

The remediation prints the suggested `mv ${stale_path} ${stale_path}.bak` command. **Doctor never executes the move**, even with `--fix`. Phantom DBs often hold partial writes from a stale watcher; the operator must verify the configured DB has the data they expect before any move.

To pin the search roots on production hosts (or to isolate tests):

```yaml
# palinode.config.yaml
doctor:
  search_roots:
    - /srv/palinode/data
    - /opt/palinode-archive
```

When `doctor.search_roots` is non-empty, **only** the listed paths are searched — the built-in list is bypassed.

#### `multiple_palinode_dirs`

Compares `$PALINODE_DIR` against `config.memory_dir`. Env vars override YAML inside `load_config()`, so editing YAML and forgetting to unset the env var is a common silent footgun. When the env var is unset, the check passes with an info-style message. When set and matches, also passes. When set and differs, warns and tells you exactly which line to edit and which env var to unset.

### Service health

Network checks. Tagged `deep`, so the fast MCP tool and `GET /doctor?fast=true` skip them.

| Check | Severity | Catches |
|---|---|---|
| `api_reachable` | error | `palinode-api` not running, or returns non-200 |
| `api_status_consistent` | error | API `/status` reports chunk count that disagrees with the on-disk DB |
| `watcher_alive` | error | `palinode-watcher` not running |
| `watcher_indexes_correct_db` | critical | Watcher process has stale `PALINODE_DIR` in its env |

#### `api_reachable`

GETs `http://${services.api.host}:${services.api.port}/health` with a 2s timeout. Any exception or non-200 response is an error with the systemd-status command in the remediation.

#### `api_status_consistent`

Compares the API's reported chunk count (`/status.chunks`) against a direct SQLite open of the configured `db_path`. This catches the case where the API is talking to the wrong or an empty database after a path change. Tolerance: the check allows the API count to be greater than the disk count (consolidation legitimately compresses many files into fewer chunks); the reverse is always suspicious.

Fix is by restart, not by data motion: `systemctl --user restart palinode-api`. If the divergence persists, the configured `db_path` itself is wrong — see `phantom_db_files`.

#### `watcher_alive`

On Linux, prefers `systemctl --user is-active palinode-watcher.service` and falls back to scanning `ps -ef` for a process whose command line contains `palinode.indexer.watcher`. On macOS, only the `ps` scan is available because Palinode does not currently ship a launchd unit.

#### `watcher_indexes_correct_db`

The check that catches the four-hour incident pattern. Reads `/proc/<pid>/environ` for the watcher process and compares its `PALINODE_DIR` against the doctor's currently-resolved value. When they differ, severity is **critical**: the watcher is still writing to the old store and new saves are silently going to a phantom DB.

Remediation: `systemctl --user restart palinode-watcher`. If that does not pick up the new env, the systemd unit's `Environment=` block is stale — compare it with the templates under `deploy/systemd/`.

**macOS limitation.** `/proc` does not exist on macOS, so this check returns `severity=info` with a "not supported on macOS" message. `ps -Eww -p <pid>` exists but requires SIP-relaxed permissions and is not portable across versions. Proper support is planned alongside a launchd unit.

### Config drift

Config-vs-runtime consistency checks. All `fast` (no network).

| Check | Severity | Catches |
|---|---|---|
| `env_vs_yaml_consistency` | warn | An env var is overriding a non-default YAML value |
| `mcp_config_homes` | warn | Multiple MCP client config files have divergent `palinode` entries |
| `process_env_drift` | warn / info | A running palinode-{api,mcp,watcher} has stale `PALINODE_DIR` |

#### `env_vs_yaml_consistency`

Re-reads `palinode.config.yaml` directly (without going through `load_config`) and compares each YAML value against the corresponding env-var override:

- `PALINODE_DIR` vs `memory_dir`
- `OLLAMA_URL` vs `embeddings.primary.url`
- `EMBEDDING_MODEL` vs `embeddings.primary.model`

Bare defaults are not flagged — only when YAML *explicitly* sets a non-default value that the env var shadows. The remediation tells you which one is winning (env always wins post-`load_config`) and lists the line to edit or the env var to unset.

#### `mcp_config_homes`

Reuses the canonical-locations walker from `palinode mcp-config --diagnose` to inspect every MCP client config home: `~/.claude.json`, `~/Library/Application Support/Claude/claude_desktop_config.json`, the 3p variant, the integration fallback, the Linux equivalent, project-local `.mcp.json`, and others. For each file present, it extracts the `palinode` server entry and warns when multiple files have entries whose contents disagree.

This is the "I edited the wrong MCP config file" footgun. Two configs both containing palinode entries are not necessarily wrong (a global plus a project-local is a legitimate setup) — the warning fires when the entries *differ*. See `docs/MCP-CONFIG-HOMES.md` for the full canonical-location matrix per client/platform.

#### `process_env_drift`

For every running palinode-{api,mcp,watcher}, reads `/proc/<pid>/environ` (Linux) and compares its `PALINODE_DIR` against the resolved config value. False-positive avoidance is built in:

- One process of a given kind with drift → **warn** (high confidence: rename-and-forgot).
- Two or more of the same kind running → **info**: treated as a deliberate side-by-side setup (test + prod, two memory dirs).
- macOS / Windows / anywhere without `/proc` → **info**, declined with a clear message.

When the API runs the check on itself (`GET /doctor` from inside the API process), it skips its own PID — the API's environ is necessarily what the API sees, so the comparison is meaningless.

### Index sanity

| Check | Severity | Catches |
|---|---|---|
| `chunks_match_md_count` | warn (error if ratio < 0.5) | Partial reindex, fresh empty DB, or watcher stopped early |
| `db_size_sanity` | warn | DB has shrunk by >50% since last doctor run (the phantom-empty-DB signature) |
| `reindex_in_progress` | info / warn | A reindex is currently running (or appears stuck) |

#### `chunks_match_md_count`

Counts `*.md` files under `memory_dir` and compares to the `chunks` row count in the configured DB. Consolidation legitimately compresses many files into fewer chunks, so the check tolerates `chunks > 0` even with a low ratio. Below 0.5 the severity escalates to error.

The remediation tells you to confirm no reindex is currently running (via `reindex_in_progress`) before running `palinode reindex` — otherwise two reindexes can race.

#### `db_size_sanity`

Maintains an append-only baseline log at `${memory_dir}/.palinode/db_size.log`, one line per doctor run:

```
<ISO-8601-UTC-timestamp> <size-bytes> <chunks-count>
```

On first run, records a baseline and passes. On subsequent runs, warns if the current DB size is less than 50% of the most recent recorded size — the "phantom empty DB" signature where a fresh zero-byte DB silently replaced the real one. Best-effort: a log-write failure does not break the check.

If the warning fires after a deliberate consolidation that legitimately shrank the DB, simply truncate the log (`> $memory_dir/.palinode/db_size.log`) and re-run doctor to record a new baseline.

#### `reindex_in_progress`

Queries the API's `/status` endpoint and reports whether a reindex is running. Severity is `info` for both "idle" and "running"; it only escalates to `warn` when a reindex has been running for more than 30 minutes (likely stuck). When the API is unreachable, the check degrades gracefully to "unknown" rather than reporting a false alarm.

This check is also load-bearing as context for `chunks_match_md_count`: a low chunk count during an active reindex is normal, not a fault.

### Disk and backup

| Check | Severity | Catches |
|---|---|---|
| `git_remote_health` | warn | Memory store has no offsite backup, or unpushed drift > 50 commits |
| `audit_log_writable` | warn | `audit.log_path` is relative (logs scatter across cwds) or unwritable |

#### `git_remote_health`

Runs `git -C ${memory_dir} ls-remote origin HEAD` with an 8s timeout. Tagged `deep` because it makes network calls.

- Pass: remote reachable; reports unpushed commit count.
- Warn: remote unreachable (DNS, SSH key, URL, transient network) or unpushed count > 50.
- Info: `memory_dir` is not a git repo, or has no remote configured. This is not a failure — offline-only stores are valid — but the check surfaces the absence of an offsite backup channel as a forward-looking risk.

#### `audit_log_writable`

When `audit.enabled=true`, validates that `audit.log_path` is absolute and that its parent directory is writable. The default value (`.audit/mcp-calls.jsonl`) is *relative*, which means every directory `palinode` is invoked from gets its own log file — a silent-misconfiguration footgun. The check warns until the path is made absolute.

**Fixable via `--fix`** (creates the parent directory when the path is relative-and-anchorable under `memory_dir`; never creates the log file itself, never edits `palinode.config.yaml`).

### Forward-looking

| Check | Severity | Catches |
|---|---|---|
| `claude_md_palinode_block` | warn | Neither the global nor any project `CLAUDE.md` mentions palinode |

#### `claude_md_palinode_block`

Walks `~/.claude/CLAUDE.md` (the global Claude Code config) and every `CLAUDE.md` in cwd and its ancestors up to `$HOME`. Warns when *none* of them mention palinode (case-insensitive substring match).

Rationale: this is the #1 install-day footgun for self-hosted users. The MCP tools register and work fine, but the LLM never reaches for them at session boundaries unless told to. A passing palinode install with no CLAUDE.md mention is technically correct and operationally invisible.

**Fixable via `--fix`** (appends a Memory (Palinode) block to an *existing* `CLAUDE.md` in cwd — never creates the file from scratch; that file is user-owned).

---

## The `--fix` whitelist

`--fix` is a deliberately narrow surface. Doctor is reached for *while degraded reasoning is happening*, and any data-touching action under that condition is unsafe. The whitelist is locked at three actions, all of which create directories or append to files; none move user data.

### What `--fix` CAN do

| Check | Action |
|---|---|
| `memory_dir_exists` | Create the configured `memory_dir` (with parents) when it is missing. |
| `audit_log_writable` | When `audit.log_path` is relative and missing, create its parent directory anchored under `memory_dir`. Never creates the log file itself — the audit subsystem owns that. |
| `claude_md_palinode_block` | Append a Memory (Palinode) block to an *existing* `CLAUDE.md` in the current directory. Never creates `CLAUDE.md` from nothing. Idempotent — declines if a `Memory (Palinode)` header already exists. |

### What `--fix` CANNOT do

It cannot:

- Move or delete `.palinode.db` files (including phantom ones). Phantom DBs may contain partial writes from a stale watcher; the operator must inspect them first. The remediation prints a suggested `mv … .bak` command but doctor never executes it.
- Edit `palinode.config.yaml`, `.mcp.json`, or any client MCP config file. Editing user config from a degraded state is exactly the wrong move.
- Restart `palinode-api`, `palinode-watcher`, or any other service. Process lifecycle belongs to systemd / launchd / the operator.
- Run `palinode reindex` or `palinode rebuild-fts`. These are heavy operations that should be invoked deliberately.

The principle is conservative-by-default: doctor's worst-case output is a verbose "everything is fine," not "I fixed something invisibly."

### `--fix` flow

```bash
palinode doctor --fix
```

For each failed check:

- **No fix registered** → prints the manual remediation and continues.
- **Fix registered** → prints the fix's verb-phrase and prompts `Apply fix? [y/N]`. Default-decline on EOF or empty input.
- **`--yes`** → apply without prompting (CI-friendly).
- **`--dry-run`** → print what *would* be done; change nothing.

Fix exceptions are caught and reported (a buggy fix never crashes doctor). After `--fix` runs, doctor exits based on the *original* check results — re-run `palinode doctor` to confirm the fixes took effect. This is intentional: re-running expensive deep checks automatically would be a footgun.

### `--json` and `--fix` together

`--fix` is suppressed in `--json` mode (interactive prompts conflict with structured output). A note is printed to stderr and the JSON is emitted normally.

---

## MCP tools

Two tools are registered. The names disambiguate intent so the LLM picks correctly without reading argument descriptions.

### `palinode_doctor` (fast)

Calls `GET /doctor?fast=true`. Runs only checks tagged `fast`: pure filesystem, pure SQLite, no network probes, no canary writes. Target: under 500ms. Use this when the user asks "what's wrong with palinode?" or "why isn't search returning anything?" — almost every silent-misconfiguration bug is detectable from disk state alone.

### `palinode_doctor_deep`

Calls `GET /doctor?canary=true` (full run, network probes included). Takes 10–15s in the steady state. Use this when:

- The fast subset reports unclear results.
- You need to verify the API and watcher are actually responding, not just configured correctly.
- You are about to claim "everything's fine" and want to back the claim with the full surface.

Both tools are read-only. `--fix` is never available via MCP — fixes are CLI-only by design.

### When the LLM should call which

A reasonable default: try `palinode_doctor` first; escalate to `palinode_doctor_deep` only when the fast result is ambiguous or the user explicitly asks for a full check. Calling `_deep` reflexively burns ~10s on every "is it working?" question; calling `fast` first respects the user's time and catches the great majority of failures.

---

## Common failure modes and how doctor catches them

These are the recurring incident shapes that motivated each check. Names are anonymized; each is a real pattern.

### After a directory rename: phantom-DB drift

**Shape.** An operator renamed the data directory and updated `PALINODE_DIR`, but `db_path` in `palinode.config.yaml` still pointed at the old location. The watcher restarted and silently auto-created a fresh empty DB at the renamed-away path. The real DB sat untouched at the old path. The API connected to whichever DB the runtime resolved first; search returned partial or empty results for hours.

**Which checks catch it.**

- `db_path_under_memory_dir` — warns immediately: `db_path` resolves outside `memory_dir`.
- `phantom_db_files` — critical: surfaces every `.palinode.db` not at the configured path, with size and chunk count for each.
- `watcher_indexes_correct_db` — critical: surfaces a watcher whose process env disagrees with the resolved config.
- `multiple_palinode_dirs` — warn: `PALINODE_DIR` env vs YAML `memory_dir` mismatch.

**Resolution.** Inspect each phantom DB (`palinode doctor --check phantom_db_files --verbose`), confirm the configured DB has the data you expect, then `mv ${stale} ${stale}.bak`. Edit `palinode.config.yaml` so `db_path` is under `memory_dir`. Restart the watcher.

### The "0 chunks" alarm

**Shape.** Post-deploy, `/health` reported `chunks=0` while the on-disk DB clearly had thousands of rows. The endpoint was correct in that it was reading *some* DB; it just wasn't the configured one.

**Which checks catch it.**

- `api_status_consistent` — error: compares `/status.chunks` against a direct SQLite open of `config.db_path`.
- `chunks_match_md_count` — warn or error depending on ratio: catches "fresh empty DB" against a non-empty `memory_dir`.
- `db_size_sanity` — warn: catches the DB-size cliff between baseline and current.

**Resolution.** Restart `palinode-api` if the running process has stale `db_path`. If that does not resolve it, the configured `db_path` itself is wrong — fall through to the phantom-DB workflow above.

### "MCP edits had no effect"

**Shape.** Operator edits `~/.claude.json` to add or fix a `palinode` server entry. Restarts Claude Desktop. Nothing changes. Eventually discovers Claude Desktop on macOS reads `~/Library/Application Support/Claude/claude_desktop_config.json`, not `~/.claude.json`.

**Which checks catch it.**

- `mcp_config_homes` — warn: lists every config file with a palinode entry and notes when entries diverge.

**Resolution.** `palinode mcp-config --diagnose` for the full table. Edit the file your client actually reads (see `docs/MCP-CONFIG-HOMES.md` for the canonical location per client/platform). Consider removing stale entries from the other files.

### "Tools work but the LLM never uses them"

**Shape.** `palinode_search` and `palinode_save` are registered and respond correctly when invoked manually, but the LLM never calls them at session boundaries. Memory feels absent without ever failing.

**Which checks catch it.**

- `claude_md_palinode_block` — warn: neither the global `~/.claude/CLAUDE.md` nor any project `CLAUDE.md` in scope mentions palinode.

**Resolution.** `palinode init` scaffolds the Memory (Palinode) block; `palinode doctor --fix` appends it to an existing `CLAUDE.md`. The block tells the LLM to call `palinode_search` at session start, `palinode_save` at milestones, and `palinode_session_end` before `/clear`.

---

## Output formats

### Text (default)

```
Palinode Diagnostics

  ✓ memory_dir_exists: Memory directory exists: /home/user/palinode-data
  ✓ db_path_resolvable: db_path is openable as SQLite (read-only)
  ⚠ db_path_under_memory_dir: db_path is outside memory_dir
      Update db_path in palinode.config.yaml to a path inside memory_dir,
      then restart palinode-api.
  ✗ phantom_db_files: 1 stale .palinode.db file outside configured path
      Move or back up these files. The watcher or a stale process may
      have written to one of them with stale env. Verify the configured
      DB at /home/user/palinode-data/.palinode.db contains the data you
      expect, then `mv /home/user/old-data/.palinode.db
      /home/user/old-data/.palinode.db.bak`. Never delete without backup.
  ...

Diagnosis: Critical issues detected.
```

Markers: `✓` green (passed), `⚠` yellow (warn), `✗` red (error/critical), `ⓘ` blue (info). Color is suppressed when stdout is not a TTY (piped, CI). Remediation prints for any failed check; `--verbose` also shows it for passing checks.

### `--json`

The JSON body is a flat array of check-result objects:

```json
[
  {
    "name": "phantom_db_files",
    "severity": "critical",
    "passed": false,
    "message": "1 stale .palinode.db file(s) outside configured path",
    "remediation": "Move or back up these files. ..."
  },
  {
    "name": "memory_dir_exists",
    "severity": "critical",
    "passed": true,
    "message": "Memory directory exists: /home/user/palinode-data",
    "remediation": null
  }
]
```

The schema is intentionally minimal and stable. `severity` is one of `info|warn|error|critical`.

### `GET /doctor`

The API endpoint wraps the same array in a summary envelope:

```json
{
  "results": [
    { "name": "...", "severity": "...", "passed": true, "message": "...", "remediation": null }
  ],
  "summary": {
    "total": 18,
    "passed": 17,
    "failed": 1
  },
  "params": {
    "fast": false,
    "canary": false
  }
}
```

Query params:

- `fast=true` — run only checks tagged `fast`. Skips network probes. Target: sub-500ms.
- `canary=false` — accepted for forward-compatibility with deep canary-write checks; currently a no-op pass-through.

Default: full run, no canary writes.

---

## Configuration

### YAML

```yaml
# palinode.config.yaml
doctor:
  search_roots:
    - /srv/palinode/data
    - /opt/palinode-archive
```

`doctor.search_roots` extends — actually, *replaces* — the built-in plausible roots that `phantom_db_files` walks. When the list is empty (the default), the built-in roots are used. When non-empty, **only** the listed paths are searched. This lets production deployments pin the exact set of roots and lets tests isolate themselves to `tmp_path` directories.

### Environment variables

Doctor reads the same env vars the rest of Palinode honors (`PALINODE_DIR`, `OLLAMA_URL`, `EMBEDDING_MODEL`). It does not introduce its own. The `env_vs_yaml_consistency` check inspects the relationship between the env-var values and the YAML values; it does not consume any new variable.

### State files

| Path | Purpose |
|---|---|
| `${memory_dir}/.palinode/db_size.log` | Append-only baseline for `db_size_sanity`. One line per doctor run: `<UTC-ISO-8601> <size-bytes> <chunk-count>`. Truncate to reset the baseline. |

---

## Exit codes

| Code | Meaning |
|---|---|
| `0` | All checks passed (or `--fix` succeeded for every fixable failure). |
| `1` | At least one check failed, or `--fix` declined a prompt. |

The CLI uses two codes — pass or fail — rather than splitting `warn` and `error` into separate exit codes. Severity is in the JSON body for scripts that need finer granularity. CI scripts that want to gate on warnings can parse the JSON for `severity == "warn"` rather than relying on exit code alone:

```bash
palinode doctor --json | jq -e '.[] | select(.passed == false)' >/dev/null && echo "issues" || echo "clean"
```

`--fix` does not re-run checks after applying fixes; the post-fix exit code reflects the original results. Run `palinode doctor` a second time to confirm the fixes took.

---

## Related documentation

- `docs/HOW-MEMORY-WORKS.md` — what doctor is checking against.
- `docs/MCP-CONFIG-HOMES.md` — the canonical-location matrix that feeds `mcp_config_homes`.
- `docs/DEPLOYMENT-GUIDE.md` — when to run doctor in the deploy lifecycle.
- `docs/QUICKSTART.md` — first-install verification with `palinode doctor`.
- `palinode doctor` is intended to be the operator-facing surface; this guide stays focused on behavior rather than internal implementation history.
