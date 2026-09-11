# CLI reference

Every `palinode` command, in the order `palinode --help` lists them. Each entry
gives the synopsis, what the command is for, its options with defaults, and an
example. Commands that already have a longer page link to it rather than repeat
it.

`tests/test_cli_reference_docs.py` checks this page against the registered
commands in both directions, so a command cannot ship without an entry here and
an entry cannot outlive its command.

## Conventions

**The CLI wraps the REST API.** Almost every command is a thin client of
`palinode-api` (port 6340 by default), so the API must be running. The
exceptions that work without it are noted in their entries. `palinode doctor`
is the first thing to run when a command fails with a connection error.

**Output is TTY-aware.** Commands with a `--format [json|text]` option default
to human-readable text when stdout is a terminal and to JSON when piped or
redirected, so `palinode status | jq .` just works. Pass `--format` to force
one. Entries below say which of four patterns a command follows:

| Pattern | Behaviour |
|---|---|
| **auto** | `--format` optional; text on a TTY, JSON when piped |
| **`--json` flag, auto** | no `--format`; a `--json` flag, and JSON also when piped |
| **`--json` flag only** | JSON only with the explicit flag, never by TTY detection |
| **text only** | no machine-readable form (or a fixed default noted in the entry) |

**Paths are relative to the memory directory.** `FILE_PATH` arguments such as
`decisions/cli-pivot.md` are resolved under `PALINODE_DIR`, never as absolute
paths.

**Dry-run by default** is the rule for anything that rewrites files in bulk:
`repair-status`, `rollback`, `import from-vault`, `obsidian-sync`,
`migrate frontmatter`, and `worktree-reconcile` all report first and need an
explicit `--execute`, `--apply`, or `--no-dry-run` to write.

Global options: `palinode --version` prints the banner and version;
`palinode --help` (or `palinode <command> --help`) prints usage.

---

## Commands

### `palinode archive`

```
palinode archive [OPTIONS] FILE_PATH
```

Retire a specific memory: archive it, or supersede it with a replacement. Sets
`status: archived` so the memory leaves default recall, records the reason in
the `-history.md` audit sibling, and commits both. Never hard-deletes — the
content stays on disk, in git, and in the index. The inverse is
[`palinode restore`](#palinode-restore). See
[DATA-LIFECYCLE.md](DATA-LIFECYCLE.md) for how archival relates to erasure.

| Option | Default | Meaning |
|---|---|---|
| `--reason TEXT` | none | Why this memory is being retired |
| `--superseded-by TEXT` | none | Slug or path of the replacement (makes it a SUPERSEDE) |
| `--format [json\|text]` | auto | Output format |

```bash
palinode archive insights/stale-finding.md --reason "superseded by the re-run" \
  --superseded-by insights/corrected-finding.md
```

Output: **auto**.

### `palinode archive-expired`

```
palinode archive-expired [OPTIONS]
```

Archive ephemeral memories whose frontmatter `expires_at` has passed, and
disable triggers whose `expires_at` has passed. Run it from cron or after a
long gap; nothing expires on its own without this sweep.

| Option | Default | Meaning |
|---|---|---|
| `--dry-run` | off | Preview which memories would be archived |
| `--format [json\|text]` | auto | Output format |

```bash
palinode archive-expired --dry-run
```

Output: **auto**.

### `palinode banner`

```
palinode banner
```

Print the Palinode ASCII brand mark. Works without the API. Output: **text
only**.

### `palinode blame`

```
palinode blame [OPTIONS] FILE_PATH
```

Show when each line of a memory file was last changed — `git blame` with the
memory's origin provenance folded in. `--claims` additionally resolves the
file's claim-level source anchors (recorded with `palinode save --claim`) and
reports the integrity status of each cited passage. Longer treatment in
[GIT-MEMORY.md](GIT-MEMORY.md) and
[HOW-MEMORY-WORKS.md §9](HOW-MEMORY-WORKS.md#9-memory-provenance-the-git-chain).

| Option | Default | Meaning |
|---|---|---|
| `--search TEXT` | none | Filter to matching lines |
| `--claims` | off | Also resolve claim-level source anchors with live integrity status |

```bash
palinode blame decisions/auth-migration.md --search "session tokens"
```

Output: **text only**.

### `palinode bootstrap-ids`

```
palinode bootstrap-ids [OPTIONS]
```

Operator maintenance: walk `people/`, `projects/`, `decisions/`, and
`insights/` and add a stable fact ID to every fact that lacks one. Needed once
when adopting consolidation on a store written before fact IDs existed; a no-op
afterwards.

`--file` narrows it to one document, which is the usual case: `palinode doctor`
names a consolidation target carrying no markers (`consolidation_targets_tagged`)
and this tags exactly that file. The path is relative to the memory dir and is
rejected if it resolves outside it.

| Option | Default | Meaning |
|---|---|---|
| `--file REL_PATH` | — | Tag one memory file instead of walking the store |
| `--format [json\|text]` | auto | Output format |

```bash
palinode bootstrap-ids
palinode bootstrap-ids --file projects/palinode-status.md
```

Output: **auto**.

### `palinode cluster-neighbors`

```
palinode cluster-neighbors [OPTIONS]
```

Find files semantically related to `--file` that are not already linked to or
from it by a `[[wikilink]]`. One of the wiki-maintenance embedding tools; see
[OBSIDIAN.md — the embedding tools](OBSIDIAN.md#the-embedding-tools). Results
are sorted by similarity, descending.

| Option | Default | Meaning |
|---|---|---|
| `--file TEXT` | required | Memory file (relative to the memory dir) to find unlinked neighbours for |
| `--min-similarity FLOAT` | 0.70 | Minimum cosine similarity to surface |
| `--top-k INTEGER` | 10 | Maximum candidates to return |
| `--format [json\|text]` | auto | Output format |

```bash
palinode cluster-neighbors --file projects/checkout.md --top-k 5
```

Output: **auto**.

### `palinode config`

```
palinode config COMMAND
```

Manage Palinode configuration. Both subcommands work without the API. The
config file is `palinode.config.yaml`; the README's "Configuration" section
lists the keys.

#### `palinode config edit`

```
palinode config edit
```

Open the configuration file in `$EDITOR` (falls back to `vi`). Resolution
order: `$PALINODE_CONFIG`, then `palinode.config.yaml` in the current
directory, then `palinode.config.yaml` in the memory directory; exits 1 if
none exists.

```bash
EDITOR=code palinode config edit
```

Output: **text only**.

#### `palinode config view`

```
palinode config view [OPTIONS]
```

Print the effective configuration — file values merged with environment
overrides and built-in defaults — as syntax-highlighted YAML or JSON.

| Option | Default | Meaning |
|---|---|---|
| `--format [json\|yaml]` | `yaml` | Output format |

```bash
palinode config view --format json
```

Output: fixed default (`yaml`); not TTY-aware.

### `palinode consolidate`

```
palinode consolidate [OPTIONS]
```

Run or preview memory compaction. The LLM proposes structured operations
(KEEP / UPDATE / MERGE / SUPERSEDE / ARCHIVE) and a deterministic executor
validates and applies them, then commits — so every pass is a git commit you
can review or revert. Full description in
[HOW-MEMORY-WORKS.md §4](HOW-MEMORY-WORKS.md#4-weekly-consolidation-sunday-3am-utc)
and [EXECUTOR-SPEC.md](EXECUTOR-SPEC.md). `palinode dream` is an alias.

| Option | Default | Meaning |
|---|---|---|
| `--nightly` | off | Lightweight nightly pass (today only, UPDATE/SUPERSEDE) |
| `--dry-run` | off | Preview the proposed operations without applying |
| `--source DIR` | `daily/` | Memory directory to consolidate; repeatable |
| `--respect-gate` | off | Apply the activity gate the cron path uses; skip and report when a pass is not yet due |
| `--format [json\|text]` | auto | Output format |

This command runs unconditionally. The activity gate
(`consolidation.auto_gate`, see [OPERATIONS.md](OPERATIONS.md#consolidation-scheduling))
governs the automatic cron path, not an operator who has asked for a pass;
`--respect-gate` opts this run into the same policy and reports
`{"status": "deferred", "gate": {…}}` when the gate is unmet.

```bash
palinode consolidate --dry-run
palinode consolidate --nightly
palinode consolidate --respect-gate
```

**This command waits for minutes, not seconds.** A pass that reaches the LLM
gets 600 s per project group server-side, so the client waits
`PALINODE_CONSOLIDATE_TIMEOUT` seconds (default `900`) rather than the 30 s
every other route uses. Raise it for a large store with many project groups.

If the wait is still not enough, the CLI stops waiting — it does not cancel
anything. The server finishes the pass and holds the store's run lock
(`.palinode/consolidation.lock`) until it does, so a second `palinode
consolidate` returns `409 Consolidation is already running (pid=…)` in the
meantime, and that run's result is only in the API log and
`logs/consolidation.log`. The command says so and exits 1; under `--format
json` it emits `{"status": "timeout", "server_still_running": true, …}`.

Output: **auto**.

### `palinode dedup-suggest`

```
palinode dedup-suggest [OPTIONS]
```

Find existing memory files semantically near a draft, to decide "create new"
versus "update existing" before saving. Candidates flagged `STRONG-DUP`
(similarity ≥ 0.90) are near-paraphrases. Wikilinks and the auto-generated
`## See also` footer are stripped before comparison so notes that merely share
entities do not false-positive. See
[OBSIDIAN.md — the embedding tools](OBSIDIAN.md#the-embedding-tools).

| Option | Default | Meaning |
|---|---|---|
| `--content TEXT` | none | Draft content to check (mutually exclusive with `--file`) |
| `--file FILE` | none | Read the draft from a file instead |
| `--min-similarity FLOAT` | 0.80 | Minimum cosine similarity to surface |
| `--top-k INTEGER` | 5 | Maximum candidates to return |
| `--format [json\|text]` | auto | Output format |

```bash
palinode dedup-suggest --content "We chose SQLite over Postgres for the cache layer"
```

Output: **auto**.

### `palinode depends`

```
palinode depends [OPTIONS] [SLUG]
```

Show the dependency tree for a milestone or task slug from the `depends_on` /
`blocks` / `parallel_with` frontmatter on ProjectSnapshots, plus whether it is
unblocked. `--unblocked` lists everything ready to start (every `depends_on`
is `status: done`). Frontmatter contract in
[HOW-MEMORY-WORKS.md §7e](HOW-MEMORY-WORKS.md#7e-dependency-frontmatter-projectsnapshot).

| Option | Default | Meaning |
|---|---|---|
| `--unblocked` | off | List all slugs whose every `depends_on` is done |
| `--format [json\|text]` | auto | Output format |

```bash
palinode depends milestone/M1
palinode depends --unblocked
```

Output: **auto**.

### `palinode diff`

```
palinode diff [OPTIONS]
```

Show what changed across memory files in the last N days, from git. See
[GIT-MEMORY.md](GIT-MEMORY.md).

| Option | Default | Meaning |
|---|---|---|
| `--days INTEGER` | 7 | Look back N days |
| `--paths TEXT` | all | Comma-separated path filters, e.g. `projects/,decisions/` |
| `--format [json\|text]` | auto | Output format |

```bash
palinode diff --days 14 --paths decisions/
```

Output: **auto**.

### `palinode doctor`

```
palinode doctor [OPTIONS]
```

Connectivity and health check — paths, services, config consistency, index
health, and disk state, each reported pass/warn/fail with remediation. The one
command to run after every install, upgrade, or server move. Works without the
API (it is one of the things it checks). Full check catalog and `--fix`
reference in [DOCTOR.md](DOCTOR.md).

| Option | Default | Meaning |
|---|---|---|
| `--json` | off | Output results as JSON |
| `--check NAME` | all | Run a single named check |
| `-v, --verbose` | off | Show remediation for all checks, not just failures |
| `--fix` | off | Apply safe automated fixes for failed checks (whitelist only) |
| `-y, --yes` | off | Skip confirmation prompts with `--fix` (CI-friendly) |
| `--dry-run` | off | With `--fix`, report what would be fixed without applying |

```bash
palinode doctor
palinode doctor --fix --dry-run
palinode doctor --json | jq '.checks[] | select(.status != "pass")'
```

Output: **`--json` flag only**. `--fix` is not applied in `--json` mode.

### `palinode dream`

```
palinode dream [OPTIONS]
```

Alias for [`palinode consolidate`](#palinode-consolidate); identical options
and behaviour. `consolidate` is the canonical name.

### `palinode entities`

```
palinode entities [OPTIONS] [ENTITY]
```

With no argument, list every entity in the reverse index built from
`entities:` frontmatter. With an entity ref such as `person/alice-smith`,
return the files that reference it. How the index is built is in
[HOW-MEMORY-WORKS.md §7](HOW-MEMORY-WORKS.md#7-entity-linking).

| Option | Default | Meaning |
|---|---|---|
| `--format [json\|text]` | auto | Output format |

```bash
palinode entities
palinode entities project/checkout
```

Output: **auto**.

### `palinode forget-withdraw`

```
palinode forget-withdraw [OPTIONS] FILE_PATH
```

Take a forget request back. `FILE_PATH` is the memory holding the request
("please forget that I…"). Restores every memory the request archived,
un-strikes every mention it retracted, and archives the request record(s) so
they stop acting as the retraction. Each step is its own audited commit;
failures are reported per target. Composes
[`restore`](#palinode-restore) and [`unretract`](#palinode-unretract).

| Option | Default | Meaning |
|---|---|---|
| `--reason TEXT` | none | Why the forget request is being withdrawn |
| `--format [json\|text]` | auto | Output format |

```bash
palinode forget-withdraw people/me-forget-request.md --reason "asked in error"
```

Output: **auto**.

### `palinode history`

```
palinode history [OPTIONS] FILE_PATH
```

Show a file's commit history with diff stats and rename tracking. `--detail
full` adds the unified diff per commit. See [GIT-MEMORY.md](GIT-MEMORY.md).

| Option | Default | Meaning |
|---|---|---|
| `--limit INTEGER` | 20 | Max commits to show |
| `--detail [summary\|full]` | `summary` | `full` also includes the diff body per commit |

```bash
palinode history projects/checkout-status.md --limit 5 --detail full
```

Output: **auto** (no `--format` option; JSON when piped, text on a TTY).

### `palinode import`

```
palinode import COMMAND
```

Import content from external sources into the memory store.

#### `palinode import from-vault`

```
palinode import from-vault [OPTIONS]
```

Import `.md` files from an Obsidian vault. Walks the vault (skipping
`.obsidian/`, `.trash/`, and hidden directories), infers a category per file
(PARA directory, daily-note filename, frontmatter `type:`, or archive), maps
each to `<category>/<slugified-path>.md`, rewrites `[[wikilinks]]` to the new
names, and adds Palinode frontmatter without overwriting what exists. Orphaned
wikilinks are reported; run [`orphan-repair`](#palinode-orphan-repair)
afterwards. Walkthrough in
[OBSIDIAN.md — migration paths](OBSIDIAN.md#migration-paths).

| Option | Default | Meaning |
|---|---|---|
| `--from-vault DIRECTORY` | required | Source vault |
| `--into-category CATEGORY/` | inferred | Map every imported file into this one category |
| `--apply` | off (dry run) | Write files to the memory dir |
| `--overwrite` | off | Replace existing files at the destination; default skips with a warning |

```bash
palinode import from-vault --from-vault ~/Vault            # preview
palinode import from-vault --from-vault ~/Vault --apply    # write
```

Output: **text only**.

### `palinode ingest`

```
palinode ingest [OPTIONS]
```

Fetch a URL and save it as a research reference, or process every file
dropped in the store's inbox directory (`inbox/raw/` by default; processed
files move to `inbox/processed/`). One of `--url` or `--inbox` is required.

| Option | Default | Meaning |
|---|---|---|
| `--url TEXT` | none | URL to fetch and save under `research/` |
| `--name TEXT` | none | Optional title for the reference |
| `--inbox` | off | Process the inbox directory instead |
| `--format [text\|json]` | auto | Output format |

```bash
palinode ingest --url https://example.com/paper --name "Paper title"
palinode ingest --inbox
```

Output: **auto**.

### `palinode init`

```
palinode init [OPTIONS]
```

Scaffold Palinode into a project: `.claude/CLAUDE.md` memory instructions,
`.claude/settings.json` hook registration, the SessionStart / SessionEnd /
UserPromptSubmit hook scripts, `.mcp.json`, the `palinode-session` skill, and
— when the harness footprint is detected — an `AGENTS.md` block and
`.cursor/rules/palinode.md`. Works without the API. Idempotent: existing files
are preserved or appended to unless `--force`. The README's "Daily use" section
and [HARNESSES.md](HARNESSES.md) explain what each harness gets;
[OBSIDIAN.md](OBSIDIAN.md) covers `--obsidian`.

It also provisions the consolidation prompts into the **memory store** —
`$PALINODE_DIR/specs/prompts/*.md`, copied from the ones packaged with
palinode. That is where the consolidation runner reads them from and where you
edit them. Existing files are never overwritten, with or without `--force`:
`--force` means "redo the scaffolding", and a tuned prompt is not scaffolding.
Use [`palinode prompt sync`](#palinode-prompt-sync) to bring stale copies
current.

| Option | Default | Meaning |
|---|---|---|
| `--dir DIRECTORY` | current dir | Project directory to scaffold |
| `--project TEXT` | directory name | Project slug |
| `--mcp / --no-mcp` | on | Write `.mcp.json` |
| `--claudemd / --no-claudemd` | on | Write the memory block to `.claude/CLAUDE.md` |
| `--agents / --no-agents` | auto | `AGENTS.md` block; auto-on when `AGENTS.md` or `.agent/` exists |
| `--cursor / --no-cursor` | auto | `.cursor/rules/palinode.md`; auto-on when `.cursor/` exists |
| `--hook / --no-hook` | on | Install the hook scripts and `.claude/settings.json` |
| `--slash / --no-slash` | on | Install the `/wrap` slash command |
| `--wrap-policy [light\|heavy]` | `light` | `heavy` also merges, pushes, and triages before archiving |
| `--skills [none\|project\|personal\|both]` | `none` | Also install `/wrap` as a Claude Code skill |
| `--skill / --no-skill` | on | Install the `palinode-session` skill into detected harness skill paths |
| `--skill-path DIRECTORY` | auto | Override the skill install root |
| `--user` | off | Install the skill to `~/.claude/skills/` instead of project paths |
| `--obsidian / --no-obsidian` | off | Also scaffold an Obsidian vault config (`.obsidian/`, `_index.md`, `_README.md`) |
| `--force-obsidian` | off | Overwrite the Obsidian scaffold (except `workspace.json`); implies `--obsidian` |
| `--prompts / --no-prompts` | on | Provision `$PALINODE_DIR/specs/prompts/*.md` from the packaged prompts (never overwrites) |
| `--force` | off | Overwrite existing files |
| `--dry-run` | off | Print the plan without writing |

```bash
cd your-project && palinode init
palinode init --dry-run --obsidian --dir ~/palinode-memory
```

Output: **text only**.

### `palinode lint`

```
palinode lint [OPTIONS]
```

Scan memory and report every deterministic health check: missing required
frontmatter, orphaned wikilinks, stale files, unresolved `contradicts` links,
core memories without `expires_at`, relative dates that will rot, and more. A
`relative_dates` finding carries the phrase, its line, the anchor the memory is
dated by, and either the absolute date the phrase resolves to or `unresolvable`
with the reason. Falls back to a local scan if the
API is down. `--deep-contradictions` adds an LLM-confirmed semantic pass over
Decision memories. Check catalog in [DOCTOR.md — lint](DOCTOR.md#palinode-lint).

`--propose` closes the loop from *detect* to *dispose*: the deterministic
findings become consolidation operations in the executor's own vocabulary, each
carrying its rationale and the finding it came from. It is a dry run — nothing
is written. `--apply` (which implies `--propose`) hands the applicable ones to
the existing deterministic write paths, stamped with an actor of `lint` so the
history sibling and the git subject say lint proposed them, not the model.

| finding | proposed operation |
|---|---|
| stale active file, eligible document class | `ARCHIVE` the whole document |
| stale active file, excluded document class | none — reported as skipped, with the reason |
| deep contradiction pair | `PROPOSE_CONTRADICTS` on both sides |
| withdrawn `backed_by` (`stale_backing`) | `PROPOSE_UPDATE` (advisory; never applied) |
| orphaned file | `PROPOSE_UPDATE` (advisory; never applied) |
| relative date, resolvable, on a fact line | `UPDATE` rewriting the phrase to its absolute date |
| relative date, vague or off a fact line | none — reported as skipped, with the reason |

A relative date is the one place this path emits replacement text, because the
replacement is arithmetic rather than judgement: "yesterday" in a memory created
on 2026-09-10 is 2026-09-09, resolved against the memory's own `created_at` (or a
dated filename) by the same normaliser that runs at write time. Phrases that name
no day — "recently", "last week", "three weeks ago" — and phrases inside quoted
text or code are reported and skipped, never guessed at.

Age-based `ARCHIVE` is never proposed for `people/`, `projects/` or
`decisions/`, for a `PersonMemory`, or for a document declaring `core: true`,
`update_policy: replace` or `retirement_policy: superseded-only` — ADR-020:
retirement is a property of the document, not of the fact's age, and `ARCHIVE`
is the only operation that removes content from default recall. Anything
needing wording (a merged sentence, a chosen winner between two claims) stays
the LLM proposer's job and is not proposed here.

| Option | Default | Meaning |
|---|---|---|
| `--format [json\|text]` | `text` | Output format |
| `--propose` | off | Translate findings into proposed operations (dry run) |
| `--apply` | off | Apply the applicable proposals through the executor path; implies `--propose` |
| `--deep-contradictions` | off | LLM-confirmed contradiction check; needs the configured LLM endpoint |
| `--max-llm-calls INTEGER` | 50 | Hard cap on LLM calls per `--deep-contradictions` run |
| `--similarity-threshold FLOAT` | 0.75 | Cosine floor for candidate pairs in `--deep-contradictions` |

```bash
palinode lint
palinode lint --format json | jq '.orphan_links'
palinode lint --propose --format json | jq '.proposals.proposals[]'
palinode lint --apply
```

Output: fixed default (`text`); pass `--format json` explicitly when piping.

### `palinode list`

```
palinode list [OPTIONS]
```

List memory files, optionally by category or restricted to `core: true`.
Expired core memories are still listed here (they are withheld only from
injection surfaces).

| Option | Default | Meaning |
|---|---|---|
| `--category [people\|projects\|decisions\|insights\|research]` | all | Filter by memory directory |
| `--core` | off | Only core memory files |
| `--format [text\|json]` | auto | Output format |

```bash
palinode list --category decisions
palinode list --core --format json
```

Output: **auto**.

### `palinode mcp-config`

```
palinode mcp-config [OPTIONS]
```

With no flags, walk every location an MCP client might read, parse each, and
report what it finds for the `palinode` server entry — for "I edited a config
and nothing changed". With `--http` or `--stdio`, emit a ready-to-paste config
block instead. Read-only: never writes a client config file. Canonical
locations per client in [MCP-CONFIG-HOMES.md](MCP-CONFIG-HOMES.md); per-harness
recipes in [MCP-INSTALL-RECIPES.md](MCP-INSTALL-RECIPES.md).

| Option | Default | Meaning |
|---|---|---|
| `--diagnose` | on | Scan all known MCP config locations |
| `--http` | off | Emit a streamable-HTTP config block (remote server) |
| `--stdio` | off | Emit a stdio config block (local install) |
| `--url TEXT` | none | Full MCP URL for `--http`, e.g. `http://host:6341/mcp/` (overrides host/port) |
| `--host TEXT` | placeholder | Host for `--http` |
| `--port INTEGER` | 6341 | Streamable-HTTP MCP port for `--http` |
| `--bearer TEXT` | none | Optional bearer token for `--http` |
| `--json` | off | Emit results as JSON |

```bash
palinode mcp-config --diagnose
palinode mcp-config --http --host memory.example.internal > ~/.cursor/mcp.json
```

Output: **`--json` flag, auto** — when piped, only the raw block is printed.

### `palinode mcp-smoke`

```
palinode mcp-smoke [OPTIONS] [HARNESS]
```

Harness smoke checklist runbook and recorder: list supported MCP harnesses,
print the copy-paste smoke checklist for one, or record a completed run. The
checklist itself and the tiers are in [HARNESS-SMOKE.md](HARNESS-SMOKE.md).

| Option | Default | Meaning |
|---|---|---|
| `--list` | off | List all supported harnesses with their tier |
| `--json` | off | Emit a parseable JSON record for the harness |
| `--record` | off | Append a completed smoke-run record to the JSONL log |
| `--date TEXT` | today | Override the run date (`YYYY-MM-DD`) |
| `--operator TEXT` | none | Who ran the smoke test |

```bash
palinode mcp-smoke --list
palinode mcp-smoke claude-code
palinode mcp-smoke codex --record --operator alice
```

Output: **`--json` flag, auto**.

### `palinode migrate`

```
palinode migrate COMMAND
```

Migration tools for bringing memories in from other formats and for
back-filling legacy files.

#### `palinode migrate bullets`

```
palinode migrate bullets [OPTIONS] MEMORY_FILE
```

Import a flat, date-bulleted memory export (one dated bullet per line) into
Palinode memory files.

| Option | Default | Meaning |
|---|---|---|
| `--dry-run` | off | Show what would be imported without writing |
| `--format [text\|json]` | auto | Output format |

```bash
palinode migrate bullets ~/exports/memory-bullets.md --dry-run
```

Output: **auto**.

#### `palinode migrate frontmatter`

```
palinode migrate frontmatter [OPTIONS]
```

Backfill missing required frontmatter on legacy memory files. Fills only absent
fields, only from an honest derivation (directory, filename, a legacy field of
identical meaning, or git history); a field with no source is reported, never
guessed, and existing values are never overwritten, so re-running is a no-op.
Each applied file is committed on its own with the derivation in the commit
body. Works without the API.

| Option | Default | Meaning |
|---|---|---|
| `--apply` | off (dry run) | Write and commit the backfill |
| `--daily-mode [skip\|minimal]` | `skip` | `minimal` fills id, category, and dates on `daily/` notes but never a `type:` |
| `--no-commit` | off | With `--apply`, write but skip the per-file commit |
| `--format [text\|json]` | auto | Output format |

```bash
palinode migrate frontmatter            # report
palinode migrate frontmatter --apply    # write and commit
```

Output: **auto**.

#### `palinode migrate openclaw`

```
palinode migrate openclaw [OPTIONS] MEMORY_FILE
```

Import a `MEMORY.md` from OpenClaw: each `##` section becomes a memory file
with heuristic type detection (person / decision / project / insight).
`--review` confirms or changes each section's type interactively before
writing.

| Option | Default | Meaning |
|---|---|---|
| `--dry-run` | off | Show what would be imported without writing |
| `--review` | off | Interactively review and override detected types |
| `--format [text\|json]` | auto | Output format |

```bash
palinode migrate openclaw ~/openclaw/MEMORY.md --review
```

Output: **auto**.

### `palinode obsidian-sync`

```
palinode obsidian-sync [OPTIONS]
```

Backfill the wiki-contract auto-footer onto legacy memory files: when a file
has `entities:` frontmatter but no matching `[[wikilinks]]` in its body, append
the `## See also` footer so Obsidian's graph view picks the links up. Idempotent
— already-synced files and files without `entities:` are skipped. Works without
the API. The contract is described in
[OBSIDIAN.md — the wiki contract](OBSIDIAN.md#the-wiki-contract).

| Option | Default | Meaning |
|---|---|---|
| `--apply` | off (dry run) | Write changes to disk |
| `--include GLOB` | all | Restrict to files matching this glob (relative to the memory dir; `**` supported) |
| `--exclude GLOB` | none | Skip files matching this glob; applied after `--include` |

```bash
palinode obsidian-sync --include 'decisions/*.md'
palinode obsidian-sync --apply --exclude 'daily/**'
```

Output: **text only**.

### `palinode orphan-repair`

```
palinode orphan-repair [OPTIONS]
```

Find existing files semantically near a broken `[[wikilink]]` target, to
propose a redirect or to create the missing file with informed context. See
[OBSIDIAN.md — the embedding tools](OBSIDIAN.md#the-embedding-tools).

| Option | Default | Meaning |
|---|---|---|
| `--link TEXT` | required | The broken wikilink (with or without brackets) or bare slug |
| `--min-similarity FLOAT` | 0.65 | Minimum cosine similarity to surface |
| `--top-k INTEGER` | 10 | Maximum candidates to return |
| `--format [json\|text]` | auto | Output format |

```bash
palinode orphan-repair --link '[[checkout-redesign]]'
```

Output: **auto**.

### `palinode prime`

```
palinode prime [OPTIONS]
```

Show the session-start context digest for a project: recent project snapshots,
core memories, recent decisions, and open action items for the resolved scope.
It is the same digest the SessionStart hook warms and the MCP `session_init`
tool returns, so it is the quickest way to see what an agent will be handed.
Scope resolution and the phases of session recall are in
[HOW-MEMORY-WORKS.md §1](HOW-MEMORY-WORKS.md#1-session-recall-every-agent-turn).

| Option | Default | Meaning |
|---|---|---|
| `--cwd TEXT` | current dir | Working directory used to resolve the project scope |
| `-p, --project TEXT` | resolved from cwd | Explicit project slug or entity ref |
| `--format [json\|text]` | auto | Output format |

```bash
palinode prime
palinode prime -p checkout --format json
```

Output: **auto**.

### `palinode prompt`

```
palinode prompt COMMAND
```

Manage the versioned LLM prompts (extraction, compaction, update,
classification, nightly consolidation) that are themselves stored as memory
files, so a prompt change is a reviewable commit like any other.

Every subcommand reads and writes `$PALINODE_DIR/specs/prompts/*.md` — the same
copies consolidation reads and `palinode doctor`'s `prompts_current` check
compares against. There is no second prompts directory.

#### `palinode prompt activate`

```
palinode prompt activate [OPTIONS] NAME
```

Activate a prompt version; other versions for the same task are deactivated.
`NAME` is the filename without `.md` (`compaction`), and the toggle rewrites
`active:` in `$PALINODE_DIR/specs/prompts/<name>.md`.

Consolidation selects its prompt by *filename* (`compaction.md`,
`nightly-consolidation.md`), not by this flag, so `active:` records which
version you consider current rather than switching what the runner loads.

| Option | Default | Meaning |
|---|---|---|
| `--format [text\|json]` | auto | Output format |

```bash
palinode prompt activate compaction-v3
```

Output: **auto**.

#### `palinode prompt list`

```
palinode prompt list [OPTIONS]
```

List all stored prompt versions, optionally for one task.

| Option | Default | Meaning |
|---|---|---|
| `--task [compaction\|extraction\|update\|classification\|nightly-consolidation]` | all | Filter by task |
| `--format [text\|json]` | auto | Output format |

```bash
palinode prompt list --task compaction
```

Output: **auto**.

#### `palinode prompt show`

```
palinode prompt show [OPTIONS] NAME
```

Display the content of one prompt version.

| Option | Default | Meaning |
|---|---|---|
| `--format [text\|json]` | auto | Output format |

```bash
palinode prompt show compaction-v3
```

Output: **auto**.

#### `palinode prompt sync`

```
palinode prompt sync [OPTIONS]
```

Refresh `$PALINODE_DIR/specs/prompts/*.md` — the consolidation prompts the
runner reads — from the copies packaged with the installed palinode. A release
that changes a prompt changes nothing until the store's copy is refreshed, and
`palinode doctor`'s `prompts_current` check is what tells you one has fallen
behind.

Only copies that still match a version palinode released are replaced. Palinode
ships the sha256 of every prompt revision it has ever released, so a store copy
whose hash is in that list is provably untouched; anything else is your edit
and is reported as `kept-edited` and left alone. A prompt missing from the store
is provisioned.

| Option | Default | Meaning |
|---|---|---|
| `--dry-run` | off | Report what would change without writing |
| `--force` | off | Also overwrite prompts you have edited (destructive) |
| `--format [text\|json]` | auto | Output format |

```bash
palinode prompt sync --dry-run
palinode prompt sync
```

Whatever it writes is git-committed in one commit whose message names each
prompt and the version it now declares (`palinode prompt sync: refreshed
compaction.md→v3; added trajectory-extraction.md (palinode 0.19.1)`, with
`--force` recorded when you used it), so `git log -- specs/prompts` tells you
when consolidation started running a given prompt revision; set
`git.auto_commit: false` to keep the store's history yours to write.

CLI-only: it is local file maintenance on your own store, not a memory
operation, so it has no MCP or REST counterpart. Output: **auto**.

### `palinode push`

```
palinode push
```

Push the memory repository to its configured git remote — the same push
`session-end --push` performs after committing. Used for off-machine backup;
see [OPERATIONS.md — backup strategy](OPERATIONS.md#backup-strategy). Output:
**text only**.

### `palinode read`

```
palinode read [OPTIONS] FILE_PATH
```

Read one memory file. `--meta` includes the YAML frontmatter as structured
data; `--tier` controls how much of the body comes back.

| Option | Default | Meaning |
|---|---|---|
| `--format [text\|json]` | auto | Output format |
| `--meta / --no-meta` | off | Include frontmatter as structured data |
| `--tier [abstract\|overview\|full]` | `full` | `abstract` is a ~300-char summary; `overview` is frontmatter plus the head of the body |

```bash
palinode read people/alice.md
palinode read projects/checkout-status.md --meta --format json
```

Output: **auto**. In JSON mode without `--meta`, the `frontmatter` key is
omitted to match the API's no-meta shape.

### `palinode rebuild-fts`

```
palinode rebuild-fts [OPTIONS]
```

Drop and recreate the BM25 (FTS5) keyword index from the database. Fast, needs
no embedder. The fix for a corrupted keyword index; see
[OPERATIONS.md — recovery](OPERATIONS.md#recovery-scenarios).

| Option | Default | Meaning |
|---|---|---|
| `--format [json\|text]` | auto | Output format |

```bash
palinode rebuild-fts
```

Output: **auto**.

### `palinode reindex`

```
palinode reindex [OPTIONS]
```

Rescan every memory file: parse, compare each section's content hash against
the stored one, re-embed only what changed, refresh the entity graph, and
rebuild the FTS5 index. Safe on a live system; if a reindex is already running
the command reports that instead of starting another. Step-by-step in
[OPERATIONS.md — what reindex does](OPERATIONS.md#what-reindex-does).

| Option | Default | Meaning |
|---|---|---|
| `--format [json\|text]` | auto | Output format |

```bash
palinode reindex
```

Output: **auto**.

### `palinode repair-status`

```
palinode repair-status [OPTIONS] [PATHS]...
```

Repair rotted memory documents. With no `PATHS`, every `projects/*-status.md`
in the memory directory is fully repaired: the `## Consolidation Log` is
re-rendered and bounded, entities are relocated out of status prose, and the
frontmatter counts and dates are reconciled with the body. `--scope all` also strips
fact markers wrongly written into the frontmatter of every other memory file —
which is where entity-graph damage lives. Nothing is committed; review the diff
yourself. The index is repointed as part of `--execute`, so no reindex is
needed afterwards. Works without the API.

| Option | Default | Meaning |
|---|---|---|
| `--execute` | off (dry run) | Write the repaired files |
| `--max-blocks INTEGER` | 10 | Consolidation Log date blocks to keep verbatim (0 = no cap) |
| `--json` | off | Emit the per-file report as JSON |
| `--scope [status\|all]` | `status` | `all` adds the frontmatter-marker strip over every other file |

```bash
palinode repair-status                      # report only
palinode repair-status --scope all --execute
```

Output: **`--json` flag only**.

### `palinode restore`

```
palinode restore [OPTIONS] FILE_PATH
```

Bring an archived memory back into default recall — the inverse of
[`archive`](#palinode-archive) for every archive path (on-demand, forget
request, TTL expiry, consolidation). Flips `status` back to `active`, drops
`superseded_by`, records `restored_at` / `restored_from` and a history line,
and commits. Retraction markers are not un-struck (that is
[`unretract`](#palinode-unretract)) and triggers are not re-enabled.

| Option | Default | Meaning |
|---|---|---|
| `--reason TEXT` | none | Why this memory is being restored |
| `--format [json\|text]` | auto | Output format |

```bash
palinode restore insights/stale-finding.md --reason "the re-run was wrong"
```

Output: **auto**.

### `palinode retrieval-stats`

```
palinode retrieval-stats [OPTIONS]
```

Summarise retrieval activity from the instrumentation log
(`.audit/retrievals.jsonl` in the memory directory) over the last N days:
event counts, unique files retrieved, and the most-retrieved files. Retrieval
events are captured automatically by `search` and `read`. Works without the
API.

| Option | Default | Meaning |
|---|---|---|
| `--days INTEGER` | 7 | Lookback window |
| `--format [text\|json]` | `text` | Output format |

```bash
palinode retrieval-stats --days 30 --format json
```

Output: fixed default (`text`); pass `--format json` explicitly when piping.

### `palinode review`

```
palinode review [OPTIONS] [PROJECT]
```

Advisory project-memory review. Composes the deterministic health signals
scoped to `PROJECT` (a slug like `checkout` or a typed ref `project/checkout`)
and proposes corrective operations — read-only, applies nothing. Omit
`PROJECT` to review the whole store.

| Option | Default | Meaning |
|---|---|---|
| `--format [json\|text]` | auto | Output format |

```bash
palinode review checkout
```

Output: **auto**.

### `palinode rollback`

```
palinode rollback [OPTIONS] FILE_PATH [COMMIT]
```

Revert a file to a previous commit by creating a new commit — history is never
rewritten. `COMMIT` is optional; without it the file goes back one version.
Dry-run by default. See [GIT-MEMORY.md](GIT-MEMORY.md).

| Option | Default | Meaning |
|---|---|---|
| `--dry-run / --no-dry-run` | `--dry-run` | Preview the change; `--no-dry-run` applies it |

```bash
palinode rollback decisions/auth-migration.md              # preview
palinode rollback decisions/auth-migration.md abc1234 --no-dry-run
```

Output: **text only**.

### `palinode save`

```
palinode save [OPTIONS] [CONTENT]
```

Store a new memory. Content comes from the argument or `--file`; the type,
entity tags, priority, provenance links, and epistemic marker go into
frontmatter. `--ps` is shorthand for a quick mid-session ProjectSnapshot; for a
structured wrap-up with decisions and blockers use
[`session-end`](#palinode-session-end). The memory file format is in the
README; the frontmatter fields are explained in
[HOW-MEMORY-WORKS.md](HOW-MEMORY-WORKS.md).

| Option | Default | Meaning |
|---|---|---|
| `--type [PersonMemory\|Decision\|ProjectSnapshot\|Insight\|ResearchRef\|ActionItem]` | inferred | Memory type |
| `--ps` | off | Shorthand for `--type ProjectSnapshot` |
| `--entity TEXT` | none | Entity tag, e.g. `person/alice`, `project/checkout` |
| `-p, --project TEXT` | none | Project slug shorthand; `checkout` becomes entity `project/checkout` |
| `--file PATH` | none | Read content from a file instead of the argument |
| `--title TEXT` | derived | Title override |
| `--slug TEXT` | derived from content | URL-safe filename slug |
| `--core / --no-core` | unset | Mark as core (injected at every session start) |
| `--confidence FLOAT` | none | Confidence 0.0–1.0, consumed by consolidation |
| `--importance INTEGER` | none | Human-assigned priority 1–5, stored as `priority` |
| `--important` / `--critical` | off | Shortcuts for `--importance 4` / `5` |
| `--metadata-json TEXT` | none | Extra frontmatter fields as a JSON object |
| `--external-ref KEY=VALUE` | none | SDLC object reference; repeatable |
| `--source TEXT` | none | Source surface (`claude-code`, `cursor`, `api`, …) |
| `--cite REF::QUOTE` | none | Source-citation anchor; integrity hash computed on save; repeatable |
| `--claim TEXT::REF::QUOTE` | none | Claim-level source anchor; read back with `blame --claims`; repeatable |
| `--contradicts REF` | none | Typed conflict link, surfaced by `lint`; repeatable |
| `--backed-by REF` | none | Typed evidence link; repeatable |
| `--update-policy [append\|replace]` | `append` | `replace` marks a living document that re-saves update in place |
| `--epistemic [fact\|inference\|open_question\|unverified]` | unset | Kind of claim the memory makes |
| `--sync / --no-sync` | async | Run the write-time contradiction check inline and report it |
| `--format [json\|text]` | auto | Output format |

```bash
palinode save --type Decision -p checkout \
  "Chose SQLite over Postgres for the cache layer. Reason: no ops burden."
palinode save --ps "Halfway through the auth migration; mobile client next."
palinode save --file notes.md --backed-by research/paper --epistemic inference
```

Output: **auto**.

### `palinode search`

```
palinode search [OPTIONS] QUERY
```

Hybrid search — BM25 keyword plus vector similarity, fused — over the memory
store. Filters narrow by directory, type, priority, date, and recency;
`--tier` controls how much of each hit is returned. Ranking is explained in
[HOW-MEMORY-WORKS.md §1](HOW-MEMORY-WORKS.md#1-session-recall-every-agent-turn).

| Option | Default | Meaning |
|---|---|---|
| `--limit INTEGER` | 3 | Number of results |
| `--category [people\|projects\|decisions\|insights\|research]` | all | Filter by memory directory |
| `--threshold FLOAT` | from config | Similarity threshold 0.0–1.0; higher is stricter |
| `--since-days INTEGER` | none | Only memories created/updated in the last N days |
| `--types TYPE` | all | Filter by memory type; repeatable |
| `--min-priority INTEGER` | none | Only memories with priority ≥ N (missing counts as 3) |
| `--date-after TEXT` / `--date-before TEXT` | none | ISO date bounds on created/updated |
| `--include-daily` | off | Rank `daily/` session notes at full weight (default: penalised) |
| `--include-telemetry` | off | Include `metadata.kind: telemetry` writes (default: excluded) |
| `--tier [abstract\|overview\|full]` | snippet | How much of each hit to return |
| `--format [json\|text]` | auto | Output format |
| `--score / --no-score` | off | Show relevance scores |
| `--no-context` | off | Disable the ambient context boost |

```bash
palinode search "database decision for cache" --limit 5 --score
palinode search "auth" --category decisions --since-days 30 --format json
```

Output: **auto**.

### `palinode session-end`

```
palinode session-end [OPTIONS] SUMMARY
```

Capture a session's outcomes to the daily note and, with `-p`, the project's
status file: summary, decisions, blockers, and provenance about the harness
that ran the session. What it writes and where is in
[HOW-MEMORY-WORKS.md §2](HOW-MEMORY-WORKS.md#2-session-capture-end-of-every-turn).

| Option | Default | Meaning |
|---|---|---|
| `-d, --decision TEXT` | none | Key decision made; repeatable |
| `-b, --blocker TEXT` | none | Open blocker or next step; repeatable |
| `-p, --project TEXT` | none | Project slug to append status to |
| `--source TEXT` | none | Source surface |
| `--harness TEXT` | none | Harness identifier (`claude-code`, `cursor`, …) |
| `--cwd TEXT` | none | Working directory the session ran in |
| `--model TEXT` | none | Model name |
| `--trigger TEXT` | none | What triggered the save (`manual`, `wrap-slash`, `hook`, …) |
| `--session-id TEXT` | none | Opaque session id from the harness |
| `--duration-seconds INTEGER` | none | Session duration |
| `--push / --no-push` | server `auto_push` | Push the memory repo after committing |
| `--dry-run` | off | Validate and render the entry without writing, committing, or pushing |
| `--format [text\|json]` | auto | Output format |

```bash
palinode session-end "Migrated auth from JWT to session tokens" \
  -d "Session tokens stored server-side, 24h expiry" \
  -b "Update the mobile client auth flow" -p checkout
```

Output: **auto**.

### `palinode split-layers`

```
palinode split-layers [OPTIONS]
```

Operator maintenance: split every `core: true` file under `projects/` and
`people/` into Identity / Status / History layers. Used once when migrating a
store to layered core files.

| Option | Default | Meaning |
|---|---|---|
| `--format [json\|text]` | auto | Output format |

```bash
palinode split-layers
```

Output: **auto**.

### `palinode start`

```
palinode start [OPTIONS]
```

Start the API server and the file watcher in the foreground, as child
processes, until Ctrl-C. For development and for opening the local
[provenance UI](UI.md); production installs use the service units in the
README's "Running as a service" section instead, and should not start a
second copy.

| Option | Default | Meaning |
|---|---|---|
| `--watcher / --no-watcher` | on | Run the memory watcher |
| `--api / --no-api` | on | Run the API server |

```bash
palinode start
palinode start --no-watcher
```

Output: **text only**.

### `palinode status`

```
palinode status [OPTIONS]
```

System health and stats: file counts, index size, embedder and service state,
and reindex progress. For a diagnosis with remediation use
[`doctor`](#palinode-doctor).

| Option | Default | Meaning |
|---|---|---|
| `--format [json\|text]` | auto | Output format |

```bash
palinode status
```

Output: **auto**.

### `palinode stop`

```
palinode stop [OPTIONS]
```

Stop the systemd services `palinode-api.service` and `palinode-watcher.service`
via `sudo systemctl stop`. Linux/systemd only; exits 1 where `systemctl` is
absent. It does not stop a foreground `palinode start` — use Ctrl-C for that.

| Option | Default | Meaning |
|---|---|---|
| `--watcher / --no-watcher` | on | Stop the watcher service |
| `--api / --no-api` | on | Stop the API service |

```bash
palinode stop --no-watcher
```

Output: **text only**.

### `palinode topic-coverage`

```
palinode topic-coverage [OPTIONS]
```

Check whether any wiki page already covers a topic phrase, before ingesting new
content. Returns `covered: true` with the best-matching file when the topic is
well represented, or `covered: false` when it is novel. See
[OBSIDIAN.md — the embedding tools](OBSIDIAN.md#the-embedding-tools).

| Option | Default | Meaning |
|---|---|---|
| `--query TEXT` | required | Topic phrase to check |
| `--min-similarity FLOAT` | 0.78 | Minimum cosine similarity to count as covered |
| `--format [json\|text]` | auto | Output format |

```bash
palinode topic-coverage --query "machine learning deployment"
```

Output: **auto**.

### `palinode trace`

```
palinode trace [OPTIONS] FILE_PATH
```

Compose the full provenance lineage of a memory file in one view: source
citations, git blame and history, the supersession trail, typed
contradiction/evidence links, and the retrieval log. Rows whose provenance is
not captured yet render an honest "not yet captured" placeholder. The JSON form
is the object the [provenance UI](UI.md) consumes.

| Option | Default | Meaning |
|---|---|---|
| `--format [text\|json]` | auto | Output format |

```bash
palinode trace decisions/auth-migration.md
```

Output: **auto**.

### `palinode trigger`

```
palinode trigger COMMAND
```

Manage prospective-recall triggers: a memory file plus a description, and when
a conversation turn resembles the description closely enough the file is
surfaced automatically. How triggers fire is in
[HOW-MEMORY-WORKS.md — phase 4](HOW-MEMORY-WORKS.md#phase-4-prospective-triggers).

#### `palinode trigger add`

```
palinode trigger add [OPTIONS] DESCRIPTION
```

Register a trigger. `--expires-at` stops it firing after a point in time
(`archive-expired` then disables it); `--authority` records who or what
licensed it to act — stored and shown, not enforced.

| Option | Default | Meaning |
|---|---|---|
| `--file TEXT` | required | Memory file to surface |
| `--threshold FLOAT` | 0.75 | Similarity threshold 0.0–1.0 |
| `--cooldown-hours INTEGER` | 24 | Hours between firings of the same trigger |
| `--trigger-id TEXT` | generated | Stable trigger ID (UUID or slug) for re-creation / dedup |
| `--expires-at TEXT` | never | ISO-8601 timestamp after which the trigger no longer fires |
| `--authority TEXT` | none | Who or what licensed this trigger (user grant, session id, policy name) |
| `--format [json\|text]` | auto | Output format |

```bash
palinode trigger add "deploying the checkout service" \
  --file decisions/checkout-rollout-plan.md \
  --expires-at 2026-12-31T00:00:00Z --authority "user grant"
```

Output: **auto**.

#### `palinode trigger list`

```
palinode trigger list [OPTIONS]
```

List registered triggers with their thresholds, cooldowns, expiry, and
authority.

| Option | Default | Meaning |
|---|---|---|
| `--format [json\|text]` | auto | Output format |

```bash
palinode trigger list
```

Output: **auto**.

#### `palinode trigger remove`

```
palinode trigger remove [OPTIONS] TRIGGER_ID
```

Remove a trigger by ID.

| Option | Default | Meaning |
|---|---|---|
| `--format [json\|text]` | auto | Output format |

```bash
palinode trigger remove checkout-rollout
```

Output: **auto**.

### `palinode unretract`

```
palinode unretract [OPTIONS] FILE_PATH PREF
```

Withdraw one preference's mention-level retraction from one memory: un-strike
every `~~…~~ [RETRACTED …]` span that `PREF` produced in `FILE_PATH` and remove
`PREF` from the file's `retracted_prefs` record, so a later forget request for
the same pref can strike again. `PREF` is the phrase as recorded in the file's
history sibling. `status` is never changed — that is
[`restore`](#palinode-restore).

| Option | Default | Meaning |
|---|---|---|
| `--reason TEXT` | none | Why the retraction is being withdrawn |
| `--format [json\|text]` | auto | Output format |

```bash
palinode unretract people/alice.md "prefers morning meetings" --reason "asked to keep it"
```

Output: **auto**.

### `palinode worktree-reconcile`

```
palinode worktree-reconcile [OPTIONS]
```

Developer maintenance for repositories where agent sessions run in git
worktrees under `.claude/worktrees/`: reclaim worktrees whose lock PID is
dead. Only removes a worktree that is locked by a dead process, has a clean
tree, and whose branch has an upstream — the branch and its commits are
preserved. Dry-run by default. Works without the API.

| Option | Default | Meaning |
|---|---|---|
| `--execute` | off (dry run) | Unlock and remove stale worktrees |
| `--json` | off | Emit the verdicts as JSON |

```bash
palinode worktree-reconcile
palinode worktree-reconcile --execute
```

Output: **`--json` flag only**.

---

## Not commands

Two strings that look like commands in other pages are not:

- **`palinode auto-save`** is the prefix of the git commit messages the watcher
  and hooks write, not something you can run.
- **`palinode migrate-mem0`** was removed in v0.14.0; the sentence that names
  it is historical.

The other entry points — `palinode-api`, `palinode-watcher`, `palinode-mcp`,
and `palinode-mcp-http` — are separate executables for the services, covered
in the README's "Install" and "Running as a service" sections.
