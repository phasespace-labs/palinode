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

### `palinode controls`

```
palinode controls [OPTIONS] COMMAND [ARGS]...
```

API-backed capture, recall, and automatic-capture controls. This command keeps
no local pause state: every read and mutation goes to the server so other
clients see the same policy. Subcommands default to text on a TTY and JSON when
piped; pass `--format` to choose explicitly.

### `palinode controls status`

```
palinode controls status [OPTIONS]
```

Shows API control state and a first-use disclosure: effective store/project,
observed project setup (not running-client proof), destinations with credentials
and query values redacted, and the stated policy limits.

| Option | Default | Meaning |
|---|---|---|
| `--cwd DIRECTORY` | current directory | Project directory to observe and use for the content-free policy preflight |
| `--project TEXT` | none | Explicit project for the content-free policy preflight |
| `--format [json\|text]` | auto | Output format |

```bash
palinode controls status --format json
```

Output: **auto**.

### `palinode controls pause`

```
palinode controls pause [OPTIONS]
```

Pause future API capture and/or recall. The default pauses both; it cannot
withdraw context already delivered or cancel an in-flight request.

| Option | Default | Meaning |
|---|---|---|
| `--capture / --no-capture` | capture | Select capture control |
| `--recall / --no-recall` | recall | Select recall control |
| `--format [json\|text]` | auto | Output format |

```bash
palinode controls pause
```

Output: **auto**.

### `palinode controls resume`

```
palinode controls resume [OPTIONS]
```

Resume the selected future API capture and/or recall path.

| Option | Default | Meaning |
|---|---|---|
| `--capture / --no-capture` | capture | Select capture control |
| `--recall / --no-recall` | recall | Select recall control |
| `--format [json\|text]` | auto | Output format |

```bash
palinode controls resume
```

Output: **auto**.

### `palinode controls exclude-project`

```
palinode controls exclude-project [OPTIONS] PROJECT
```

Add one project to automatic capture/recall exclusions, or remove that one exclusion.
Explicit user/API writes remain allowed.

| Option | Default | Meaning |
|---|---|---|
| `--remove` | off | Remove rather than add this exclusion |
| `--format [json\|text]` | auto | Output format |

```bash
palinode controls exclude-project private-prototype
```

Output: **auto**.

### `palinode controls exclude-path`

```
palinode controls exclude-path [OPTIONS] PATH
```

Add one source path to automatic capture/recall exclusions, or remove that one
exclusion. This is not a general secret detector.

| Option | Default | Meaning |
|---|---|---|
| `--remove` | off | Remove rather than add this exclusion |
| `--format [json\|text]` | auto | Output format |

```bash
palinode controls exclude-path "$PWD/.env"
```

`PATH` must be an absolute path with no `..` or symlink component; the API
normalizes and rejects unsafe input rather than treating it as an exclusion.

Output: **auto**.

### `palinode aliases`

```
palinode aliases [OPTIONS] COMMAND [ARGS]...
```

List, edit and check the store's curated entity aliases: the groups in
`entity-aliases.yaml` at the root of the memory directory that make several
spellings of one subject count as one, for entity lookup and for project
isolation. Every subcommand goes through the API; the server decides the file's
path, writes it sorted and commits it in the store's git. Memory files are
never rewritten. See [ENTITY-ALIASES.md](ENTITY-ALIASES.md) for the format and
for which spellings to merge.

There is no MCP tool for this command, by design: alias groups decide what a
project-scoped session recalls, so changing them stays with the operator.

#### `palinode aliases list`

```
palinode aliases list [OPTIONS]
```

Every group, the canonical ref first, each ref with the number of indexed files
tagged with exactly that spelling. Counts show `?` when there is no index.

| Option | Default | Meaning |
|---|---|---|
| `--format [json\|text]` | auto | Output format |

Output: **auto**.

#### `palinode aliases add`

```
palinode aliases add [OPTIONS] CANONICAL MEMBERS...
```

Create the group `CANONICAL`, or add `MEMBERS` to it. Applies by default;
`--dry-run` prints the diff of the file and writes nothing. A member that
already belongs to another group is refused unless `--move`, which takes it out
of that group (a group left empty is removed). A ref that is another group's
canonical is always refused. Project refs compare case-insensitively for these
rules, as they do in recall isolation. The file is rewritten in a fixed order,
so comments in a hand-edited file are not kept; a malformed file is refused.

| Option | Default | Meaning |
|---|---|---|
| `--move` | off | Take a member out of the group it already belongs to |
| `--dry-run` | off | Show the change; write nothing |
| `--format [json\|text]` | auto | Output format |

```bash
palinode aliases add project/orbit-app project/orbitapp project/Orbit_App --dry-run
```

Output: **auto**.

#### `palinode aliases remove`

```
palinode aliases remove [OPTIONS] MEMBER
```

Drop `MEMBER` from its group; a group left with no members is removed. A
group's canonical cannot be removed this way: remove its members instead.

| Option | Default | Meaning |
|---|---|---|
| `--dry-run` | off | Show the change; write nothing |
| `--format [json\|text]` | auto | Output format |

Output: **auto**.

#### `palinode aliases check`

```
palinode aliases check [OPTIONS]
```

Run the alias lint (the same candidates `palinode lint` reports under
`entity_aliases`) over the index, marking each cluster that one group already
covers, and the `project_tags_unmapped` doctor check. Reports only; exits 0.

| Option | Default | Meaning |
|---|---|---|
| `--format [json\|text]` | auto | Output format |

Output: **auto**.

### `palinode archive`

```
palinode archive [OPTIONS] FILE_PATH
```

Retire a specific memory: archive it, or supersede it with a replacement. Sets
`status: archived` so the memory leaves default recall, records the reason in
the `-history.md` audit sibling, and commits both. Never hard-deletes — the
content stays on disk, in git, and in the index, and it does not reach memories
that *quote*, cite or link this one: the result names those (bounded, with an
explicit "N more"; ones you may not see are counted, not named) and leaves them
in recall, unchanged. The inverse is [`palinode restore`](#palinode-restore).
See [DATA-LIFECYCLE.md](DATA-LIFECYCLE.md#four-operations-not-one) for how
archiving, correcting, restoring and erasing differ, and
[the lifecycle in one paragraph](DATA-LIFECYCLE.md#the-lifecycle-in-one-paragraph)
for the short version.

| Option | Default | Meaning |
|---|---|---|
| `--reason TEXT` | none | Why this memory is being retired |
| `--superseded-by TEXT` | none | Slug or path of the replacement (makes it a SUPERSEDE) |
| `--dry-run` | off | Preview the frontmatter delta, the relation recorded, the retained copies and the recovery command; write nothing |
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

Run or preview memory compaction. Applied changes are validated and committed
to git, so you can review or revert them. Full description in
[HOW-MEMORY-WORKS.md §4](HOW-MEMORY-WORKS.md#4-weekly-consolidation-sunday-3am-utc)
and [EXECUTOR-SPEC.md](EXECUTOR-SPEC.md). `palinode dream` is an alias.

| Option | Default | Meaning |
|---|---|---|
| `--nightly` | off | Lightweight nightly pass (everything not yet consolidated, UPDATE/SUPERSEDE) |
| `--dry-run` | off | Preview the proposed operations without applying |
| `--source DIR` | `daily/` | Memory directory to consolidate; repeatable |
| `--respect-gate` | off | Apply the activity gate the cron path uses; skip and report when a pass is not yet due |
| `--format [json\|text]` | auto | Output format |

This command runs unconditionally. The activity gate
(`consolidation.auto_gate`, see [OPERATIONS.md](OPERATIONS.md#consolidation-scheduling))
governs the automatic cron path, not an operator who has asked for a pass;
`--respect-gate` opts this run into the same policy and reports
`{"status": "deferred", "gate": {…}}` when the gate is unmet.

`--nightly` selects the notes written since each project's last successful
pass — a watermark, not a window — so a hand-run after a failed night picks up
exactly what the failure left behind and nothing else. See
[OPERATIONS.md](OPERATIONS.md#the-nightly-does-not-have-a-window).

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

### `palinode corrections`

```
palinode corrections [OPTIONS]
```

List correction candidates mined from harness session transcripts: moments a
user overturned a decision, rejected an approach and said why, or asked for
something to be remembered. Every candidate is a **proposal** — nothing is
applied, and no memory is written. See
[HARNESSES.md — mining transcripts for corrections](HARNESSES.md#mining-transcripts-for-corrections)
for what is read, what is stored, and how to turn it on.

| Option | Default | Meaning |
|---|---|---|
| `--project SLUG` | all | Only candidates scoped to this project |
| `--since N` | all | Only candidates from the last N days; also narrows a `--scan` lookback |
| `--scan` | off | Run a detection pass over the configured transcript paths before listing |
| `--format [json\|text]` | auto | Output format |

The source is **off by default**: without `capture.transcripts.enabled` and a
`capture.transcripts.harness_paths` entry, `--scan` reads nothing, sends nothing
and the list is empty.

Classification is a **third, separate opt-in** (`capture.transcripts.classify`,
also off by default) and is the only part that transmits anything. With it off,
`--scan` runs the deterministic stage locally, makes no request to any model,
and queues every candidate as `needs_review`; the report says `detection only;
nothing was sent to a model`. With it on, each candidate's bounded window (≤5
turns, ≤400 characters each, your own turns and ordinary assistant replies only)
goes to the configured consolidation endpoint — remote if you configured a remote
one — and the report names the model and role it used. `palinode controls status`
names the destination. There is deliberately **no flag to enable classification
for one call**; it is a config decision. Listing without `--scan` sends nothing
either way. A scan honours `palinode controls` exactly as every other automatic
capture source does — paused capture stops it, an excluded project or path skips
those transcripts — and its lookback window and candidate cap report what they
skipped rather than truncating quietly.

```bash
palinode corrections --scan --since 7
palinode corrections --project checkout --format json
```

Output: **auto**.

#### `palinode corrections list`

```
palinode corrections list [OPTIONS]
```

The same listing bare `palinode corrections` prints, spelled explicitly. Takes
the identical options.

Output: **auto**.

#### `palinode corrections preview`

```
palinode corrections preview [OPTIONS]
```

Show exactly what correcting or retiring one memory would change — and change
nothing. Prints the old text, the proposed new text, the target's exact source
revision, the rationale, where the correction came from, the project scope, the
`supersedes` / `superseded_by` relation that would be recorded, every other
record that quotes or derives from the target (**reported, never rewritten**),
and the recovery command. It ends with the `apply` invocation, with the
revision baked in.

| Option | Default | Meaning |
|---|---|---|
| `--target REF` | — | Memory to correct: `decisions/x.md`, `decisions/x`, or a bare slug |
| `--claim ID` | whole document | Narrow the correction to one `<!-- fact:id -->` claim |
| `--replacement TEXT` | — | The text that would stand instead; include untouched claims |
| `--allow-content-loss` | false | Explicitly permit dropping the original text listed by preview |
| `--action [supersede\|retire]` | derived | `retire` withdraws the target with no successor |
| `--reason TEXT` | — | Why. Recorded in the history sibling and the commit subject |
| `--candidate ID` | — | The queued candidate this correction came from |
| `--project SLUG` | inferred | Project scope for the replacement |
| `--type TYPE` | inherited | Memory type for the replacement |
| `--slug SLUG` | derived | Slug for the replacement |
| `--format [json\|text]` | auto | Output format |

A bare slug that names two memories is **refused with both**, never guessed; so
is a claim id that names two lines. A correction whose source named no target
is refused too — an absent relation is the honest record of "the user did not
say which memory", and the reviewer chooses.

```bash
palinode corrections preview --target decisions/harbor-notes-storage \
  --replacement 'Use the hosted service for shared writes.' \
  --reason 'concurrent writers need transactional coordination'
```

Output: **auto**.

#### `palinode corrections apply`

```
palinode corrections apply --expect-revision REV --confirm [OPTIONS]
```

Apply a previewed correction. Takes every `preview` option plus
`--expect-revision` (required) and `--confirm` (required) — apply is never the
default on any surface. A target that changed since the preview is refused with
both revisions rather than merged.

Writes only through the paths that already exist: the replacement goes through
the same save `palinode save` uses, and the original is archived with
`superseded_by`, so it stays on disk, in git and retrievable as history. The
commit subject and the `-history.md` line name the actor as a reviewed
correction, and carry the candidate id when one was the source.

```bash
palinode corrections apply --target decisions/harbor-notes-storage \
  --replacement 'Use the hosted service for shared writes.' \
  --reason 'concurrent writers need transactional coordination' \
  --expect-revision 9f2c… --confirm
```

Output: **auto**.

#### `palinode corrections dismiss`

```
palinode corrections dismiss --candidate ID --reason TEXT [OPTIONS]
```

Record that a reviewer looked at a candidate and declined it. Writes no memory:
the queue row is marked `dismissed`, with the reason, and **kept** — which is
what stops the next transcript scan proposing the same span again. A reason is
required, because a dismissal with none is indistinguishable from a candidate
nobody ever looked at.

Output: **auto**.

#### `palinode corrections undo`

```
palinode corrections undo --target REF [OPTIONS]
```

Preview (default) or apply the undo of a correction. Without `--confirm` this
reads: it states what would be restored, what would **not** be deleted, and
what cannot be reached at all.

| Option | Default | Meaning |
|---|---|---|
| `--target REF` | — | The archived memory to restore |
| `--expect-revision REV` | — | The revision the undo preview showed; required with `--confirm` |
| `--confirm` | off | Required to write. Preview is the default |
| `--reason TEXT` | — | Why the correction is being undone |
| `--format [json\|text]` | auto | Output format |

Three different things, named separately in the output because only the first
is on offer: **restoring a previous assertion** (this command), **deleting
history** (not offered — the commits, the `-history.md` sibling and the
replacement record all remain), and **undoing an agent's external actions**
(impossible — code written, messages sent and requests made are outside this
store). It refuses to resurrect a record that was separately retracted or
withdrawn by a forget request; those have their own commands.

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

### `palinode explain`

```
palinode explain [OPTIONS] BUNDLE_ID
```

Explain one delivery of context. `BUNDLE_ID` is the reference a delivery handed
back — the receipt's `bundle_id` (the bundle's `receipt_ref`). The command reads
the rows that delivery wrote to `.audit/retrievals.jsonl` and reports which
memories were supplied, the exact revision each was supplied at and whether that
source has changed since, the server-resolved scope, the calling surface and
demand, each record's disposition, the delivery's coverage qualifiers and its
evaluation time. Resolve receipts also retain output budget, selection steps,
record roles and qualifiers, including evidence references.

Every field the log never recorded is shown as `unavailable` with the reason,
never guessed — including the difference between "no rows carry this reference"
and "this surface writes no rows at all". A search that delivered nothing is
explained as exactly that. Which surfaces can be explained after the fact, and
for how long, is in [DELIVERY-RECEIPTS.md](DELIVERY-RECEIPTS.md#explaining-a-delivery-after-the-fact).

| Option | Default | Meaning |
|---|---|---|
| `--limit INTEGER` | 20 | Supplied records to show; the rest are counted, not hidden |
| `--diagnostics` | off | Also show the delivery's query prose and session id. Served only on a loopback-bound API, or when `--session-id` matches the delivery's own; otherwise both come back withheld with the reason printed |
| `--session-id TEXT` | none | Resolve this session's scope chain; records it may not see stay redacted. Also unlocks `--diagnostics` against a remote API for that session's own deliveries |
| `--format [text\|json]` | auto | Output format |

```bash
palinode explain 3f9a1c2d5b7e4a60
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
| `--dry-run` | off | Preview each step (restores, un-strikes, request records archived) and the retained copies; write nothing |
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

Before each request — the submitted URL and every redirect target, five hops at
most — the host is resolved and every returned address must be globally
routable; a redirect into a private, loopback, link-local, or carrier-grade NAT
address is refused rather than followed. The connection is then made to the
address that was checked, not by name, so a host that answers with a public
address and then a private one cannot be reached through the gap between the
two. TLS is unaffected: the certificate is still verified against the hostname.

One exception, and it is logged when it applies: with an `HTTPS_PROXY` set, the
request is tunnelled and the connection is made by name, because a tunnel
verifies the certificate against its own target and a pinned address there
would mean checking the certificate against an IP. The address check still
runs; only the pinning is skipped.

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

**Before your first capture:** Palinode stores readable Markdown and Git history. `private`/`restricted` control discovery by scope; a caller with API access can still read a hidden memory by its known path and use full-store maintenance tools. These labels provide no encryption or per-user/per-agent authentication. Protect the store, backups and API credentials; use separate instances or filesystem permissions for stronger separation. See the [privacy contract](PRIVACY.md). `init` prints this boundary before provisioning capture hooks, including in dry runs.

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
| `--pin-project` | off | Pin the project slug as the generated client's `PALINODE_PROJECT`, so every recall call from it resolves that project without passing one. Requires `--mcp`; without the flag the emitted block is unchanged. |
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

`--propose` translates supported lint findings into consolidation operations, each
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
| `--project TEXT` | none | Safe project slug for the generated client: emitted as `PALINODE_PROJECT` for `--stdio`, and as the `X-Palinode-Project` request header for `--http` (a remote server cannot see the client's directory). |
| `--json` | off | Emit results as JSON |

```bash
palinode mcp-config --diagnose
palinode mcp-config --http --host memory.example.internal > ~/.cursor/mcp.json
palinode mcp-config --stdio --project harbor-notes
palinode mcp-config --http --host memory.example.internal --project harbor-notes
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
Retired memories (archived, superseded, deprecated, retracted or expired) never
appear; a row in open conflict, resting on a retired source, or carrying a
declared epistemic marker says so inline (`[⚠ contradicts: … | ⚠ stale
backing: … | epistemic: …]`), and the same fields are keys on the JSON output.
Scope resolution and the phases of session recall are in
[HOW-MEMORY-WORKS.md §1](HOW-MEMORY-WORKS.md#1-session-recall-every-agent-turn).

| Option | Default | Meaning |
|---|---|---|
| `--cwd TEXT` | `CWD` env, then current dir | Working directory used to resolve the project scope |
| `-p, --project TEXT` | resolved from cwd | Explicit project slug or entity ref |
| `--format [json\|text]` | auto | Output format |

```bash
palinode prime
palinode prime -p checkout --format json
```

The digest reports the project, resolution basis and whether visible current
records recognize it. See [project resolution](HOW-MEMORY-WORKS.md#choosing-the-project)
for linked worktrees, overrides, disablement and remote API paths.

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

The comparison is over the prompt **body** — the frontmatter is not hashed. A
store is a memory store, and its own machinery writes fields into prompt
frontmatter (cross-references, descriptions); the body is what consolidation
sends the model, so it is the part whose provenance this decision is about.
Adding, reordering or reformatting a frontmatter field therefore leaves a
prompt refreshable; changing a line of the prompt text is your edit.

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
| `--dry-run` | off | Preview the frontmatter delta, the relation removed, the backing that would be flagged and the recovery command; write nothing |
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

### `palinode resolve`

```
palinode resolve [OPTIONS] [QUERY]
```

Ask what memory holds **right now** — for a question, or for one record. Where
`search` returns hits and leaves the reading to you, `resolve` gathers the
evidence around each hit and reports the outcome: the assertions that stand
(with their source revisions), what replaced what, conflicts with every side
intact, and what is explicitly unknown. Read-only, and no model is involved —
with no embedder reachable it seeds from keyword search and says so in the
coverage line.

Give a `QUERY`, a `--ref`, or both. `--context` names refs you already hold:
each is checked rather than assumed current, which is how a stale ref you are
carrying comes back reported as replaced.

| Option | Default | Meaning |
|---|---|---|
| `--ref TEXT` | — | Exact memory ref (path without `.md`), e.g. `decisions/db` |
| `--context TEXT` | — | A ref you already hold; repeatable |
| `--intent [current_state]` | `current_state` | What to answer. Only current state is supported |
| `--max-items INT` | `8` | Maximum units in the answer |
| `--max-chars INT` | `2000` | Maximum characters in the rendered answer |
| `--format [json\|text]` | auto | Output format |

```bash
palinode resolve "which endpoint does production serve from"
palinode resolve --ref decisions/endpoint          # is what I'm holding still current?
palinode resolve "deploy policy" --max-chars 600   # a tight budget for an injected answer
```

Budget pressure drops whole units in priority order (standing assertions
first, then conflict groups, then replacements, then unknowns) — a conflict is
never split. A group that cannot fit is reported by ref with
`budget_exhausted:conflicts` in the coverage line, so a small answer can never
read as a settled one.

Output: **auto**.

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
| `--undo-retirements` | off | Acknowledge that the rollback undoes a retirement. Without it, a rollback that would bring an archived, superseded or retracted record back as current is refused (exit 1) and writes nothing; the preview names each such retirement. See [DATA-LIFECYCLE.md](DATA-LIFECYCLE.md#recovery-and-what-it-is-incompatible-with) |

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
| `--update-policy [append\|replace]` | unset | How a re-save to the same slug is written: `append` keeps the existing body and adds beneath it, `replace` overwrites it and marks a living document. Unset overwrites without marking |
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
| `--resolve [none\|linked\|full]` | none | Attach bounded evidence around each hit (see below) |
| `--format [json\|text]` | auto | Output format |
| `--score / --no-score` | off | Show relevance scores |
| `--no-context` | off | Disable the ambient context boost |

```bash
palinode search "database decision for cache" --limit 5 --score
palinode search "auth" --category decisions --since-days 30 --format json
palinode search "which database do we use" --resolve full
```

Text output opens with the scope the search ran under and the source that
decided it — `Scope: project/harbor-notes (environment)` for a pinned
`PALINODE_PROJECT`, `(git_origin)` for a repository-derived one, `Scope: none
(none)` when nothing resolved. `--format json --diagnostics` returns the same
two as `project` and `project_resolved_by`. Precedence and the full source
vocabulary are in
[HOW-MEMORY-WORKS.md](HOW-MEMORY-WORKS.md#choosing-the-project).

`--resolve linked` follows each hit's `superseded_by`, `contradicts` and
`backed_by` links forward and in reverse (which records name this one) under
fixed budgets, with cycle detection, so a replaced hit shows its successor and a
correction ranked below `--limit` still appears under the hit it corrects.
`--resolve full` adds bounded unlinked discovery — exact entity / source lookups,
then a keyword and a neighbour query on the hit's own identifiers and vector.
Each record is rendered as `↳ replaced by: decisions/x [current, 2026-09-10] …`
with its own currency; each hit reports `coverage` (`complete`, or `partial` with
reasons such as `budget_exhausted:edges`, `target_hidden`, `index_lag`,
`fallback_disabled`) that never names a hidden record. Read-only: nothing is
written or committed. JSON output carries the block as `evidence` per hit.

Under the evidence, each hit gets one line saying what it resolves to:

```
⇒ current: decisions/db-v2 [accepted_intent, current] — explicit_replacement, uncontested
⇒ unresolved conflict — policy_implementation_mismatch
  · side: decisions/deploy [accepted_intent, current]
  · side: observations/deploy-seen [observation, current]
⇒ insufficient evidence — replacement_withdrawn
```

Exactly one of `current`, `unresolved conflict` or `insufficient evidence`, with
reasons from a closed vocabulary. Only mechanically explicit changes resolve — a
`superseded_by` chain ending at a standing successor, a declared retirement, a
past `expires_at`. A `contradicts` link, a newer date, an `epistemic` label and
the relevance score can make a conflict visible but never pick a winner, so a
newer proposal does not replace an accepted decision and a withdrawn replacement
leaves `insufficient evidence` rather than the value it replaced. Support that
rests on one named origin (the same `sources[].ref`, claim anchor or `backed_by`
ref) is grouped and counted once. JSON output carries the block as `resolution`
per hit; the same decision is rendered identically by the MCP and REST surfaces.

Each hit is labelled with three separate provenance answers — index/source
agreement (`[index matches source]` / `[⚠ index stale]`, the stored hash against
the file; never a statement about truth), cited-span integrity (`[⚠ cited
quote: source_drifted]` when a `sources:` anchor no longer matches), and
assertion currency (`[⚠ retired: status:archived]`, `[⚠ contested]`; `current`
and `unmarked` are unlabelled). The JSON output carries them as `freshness`,
`span_integrity`, `currency` and `currency_reason`; see
[HOW-MEMORY-WORKS.md §1](HOW-MEMORY-WORKS.md#phase-2-topic-specific-search-per-message).

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

Stop the systemd services `palinode-api.service` and the watcher unit.
Linux/systemd only; exits 1 where `systemctl` is absent. It does not stop a
foreground `palinode start` — use Ctrl-C for that.

The watcher unit is **resolved, not assumed**: `palinode stop` asks
`systemctl is-active` on the system manager first and `--user` second for
`palinode-watcher.service`, `palinode-indexer.service`, and the installer's
`WATCHER_UNIT_NAME` override when it is exported, then stops whichever answered
— through `sudo systemctl` for a system unit and `systemctl --user` for a user
unit. When nothing is active it falls back to `palinode-watcher.service`. This
is the same resolution `palinode doctor`'s `watcher_alive` check uses, so the
two cannot disagree about which unit your host runs.

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
| `--dry-run` | off | Preview the spans that would be un-struck and the `retracted_prefs` delta; write nothing |
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
