# Data lifecycle: deletion, retention, and the right to erasure

Palinode's design goal is memory you can audit: files as the source of truth, every
write git-committed, contradictions recorded rather than resolved by overwriting. That
design has an obvious tension with a legal right most operators eventually meet —
**GDPR Article 17 (and its analogues): a person can require their personal data to be
erased.** An immutable history and a right to erasure cannot both be absolute.

This document is the project's honest answer: what deletion actually does today, what a
complete erasure requires, and where the residues are. It is engineering documentation,
**not legal advice** — whether a given procedure satisfies a given regulator is a
question for counsel.

## The lifecycle in one paragraph

*(Other pages link here rather than restating it.)* A memory is a markdown file. It is
**current** until something retires it: an archive, a supersession, a retraction, or a
passed `expires_at`. A retired memory is still on disk, still in git, still in the
index, and still readable on demand — it just stops being offered as something that
currently holds. Retirement is reversible (`palinode restore`). **Erasure** — actually
removing the bytes — is a separate, destructive, mostly manual procedure that no single
command performs, and it is the only thing on this page that cannot be undone.

### What "stops being offered" means on each path

**Automatic delivery follows search.** Anything Palinode hands an agent without being
asked — the per-turn hook's `POST /resolve`, the plugin's per-turn recall, and the
SessionStart prime (`/context/prime`, the core-memory digest) — leaves retired records
out by default, the same way `palinode search` does. "Retired" is the shared lifecycle
classifier's answer: archived, deprecated, superseded, retracted, past `expires_at`, or
filed under `archive/`. A retired record is not carried as an answer, as an unlinked
"also found" beside one, as an unknown, or by ref: when a current record replaced it,
the current record is delivered and its `replaces:` line says
`1 earlier record (retired; withheld)` instead of naming the old one, because a ref is
often a slug of the value it retired. The bundle's `history_withheld` counts what was
left out. A label is not enough on its own: an agent handed a retired value marked
retired still tends to answer with it.

Search's evidence mode (`resolve=linked` / `resolve=full`) follows the same default.
Agents choose it on their own, so it is not a request for history either. Each hit's
`evidence` block leaves out retired records, whether they came from unlinked
discovery, a replaced predecessor, a retired source or a conflict, and the
`resolution` block drops their refs from its sides and support groups. The block's
`history_withheld` counts them. The hit itself is never touched, and every outcome is
still decided over the whole evidence; only what is shown changes.

History stays available when someone asks for it, and it is always labelled:

- `palinode resolve --include-retired`, `palinode_resolve(include_retired=true)` and
  `POST /resolve {"include_retired": true}` put retired records back into the bundle,
  under `Replaced`, as `[retired]` discoveries, and as `retired_no_successor` /
  `expired` unknowns.
- `include_retired` on search (`palinode search --resolve full --include-retired`,
  `palinode_search`, `POST /search`, the plugin's `palinode_search`) puts them back into
  each hit's evidence, each marked `⚠ retired`.
- A record the caller names itself (`ref`, or a `context` ref it is carrying) is always
  reported. Being told that the record you hold was replaced is the reason to name it.
- `palinode history`, `palinode trace` and the inspector read a record's whole
  lineage directly.

## Four operations, not one

Almost every confusion about this subject comes from treating these as the same thing.
They are not, they have different commands, and only the last one removes anything.

| # | Operation | What it means | What it does *not* do | Command |
|---|---|---|---|---|
| 1 | **Correct** | The record was wrong, or is now wrong. Write the right one and link them. | Remove the old wording — that is the audit trail; rewrite the records that quote the target, which are reported and left alone | `palinode corrections preview` → `apply` → `undo` ([CORRECTIONS.md](CORRECTIONS.md)): previewed, revision-checked and confirmed. It composes the operations below, which remain available on their own — `palinode save --contradicts <ref>` for a conflict with no winner; `palinode archive <old> --superseded-by <new>` when the new one wins; `palinode rollback <file>` to undo a mistaken edit |
| 2 | **Archive / forget** | Stop using this by default. The memory leaves recall, resolve, priming and trigger delivery. | Delete anything, anywhere; reach the memories that quote or cite it — those are named in the result, never changed | `palinode archive <file>` (`--dry-run` to preview) · a "please forget that I…" request, which routes each resolved memory to either a whole-file archive or mention-level striking (write-time forgetting) |
| 3 | **Restore** | Undo 2. The memory comes back into default recall, visibly (`restored_at`). | Un-strike retraction markers — that is `palinode unretract <file> <pref>` — or re-enable a trigger's `enabled` flag | `palinode restore <file>` · `palinode forget-withdraw <request-file>` (each takes `--dry-run`) |
| 4 | **Erase** | The bytes must be gone, from the file, from history, and from every copy. | Happen automatically, or happen in one command | The manual runbook below |

**Supersession is not erasure.** Palinode's default for outdated or contradicted
memories is to mark them — superseded, contradicted, archived — while keeping them
retrievable. That is a *feature*: an auditor can ask what was believed and when. It is
also, deliberately, the opposite of erasure.

A retirement is honest about its own reach, and this is the part worth internalising:
**archiving a memory does not reach the memories that quote it.** A second memory
citing the first in a `sources[].quote` span holds the original wording verbatim, stays
active, and stays in recall. That is correct — it is somebody else's memory and
retiring it would take out unrelated recall — but it means "I archived that" and "that
wording is out of recall" are different sentences. Step 1 of the runbook exists for
exactly this.

So the operation says which sentence you got. The result of `archive` — and of a
forget request, and of `forget-withdraw` for the request records it retires — carries
`retained_copies`: every memory still in default recall that quotes the retired one
(`sources` / `claims`), cites it (`backed_by`, `contradicts`, `supersedes`,
`superseded_by`, `falsified_by`) or links it by `[[wikilink]]`, each with what you can
do about it. They are **reported, never changed**. At most ten are named and the rest
are counted ("… and N more"); a memory you may not see (`private` / `restricted`) is
counted and never named. It is the same discovery the correction preview uses, so the
two never disagree about what refers to what.

If what you need is the *first* operation — replacing or withdrawing a statement while
keeping it auditable — see [CORRECTIONS.md](CORRECTIONS.md), which walks through
preview, apply and recovery. Nothing on that page deletes anything.

## Where a memory actually lives

A complete erasure has to cover every copy. **Every one of them takes an operator's
step.** One — the index — rebuilds its *contents* on its own once the files are clean,
which is a smaller promise than it sounds like: see the note under the table.

| Location | What it holds | Removal | How you check absence |
|---|---|---|---|
| The markdown file | The memory itself | manual (`rm`) | `ls`, or `palinode read` returns 404 |
| A memory that **quotes** it | The original wording, verbatim, in `sources[].quote` | manual (redact in place) | grep the store for the phrase |
| `<base>-history.md` sibling | Retirement reasons — which, on the forget path, restate the retired text | manual (`rm`) | grep the store |
| **Git objects** | Every past version of every file above | **manual history rewrite — `rm` does *not* touch this** | `git cat-file --batch-all-objects` (see below) |
| **Git commit messages** | The memory's *path* — i.e. the subject's slug — in every `palinode: archive …` subject | **manual, and a path-only rewrite misses it** | `git log --all --format=%s` |
| SQLite chunks + FTS + vectors | Embeddings and search terms derived from the files | **automatic to rebuild, manual to remove** — delete `.palinode.db` **and its `-wal` / `-shm` sidecars**, then let the rebuild run | grep the raw `.palinode.db*` files for the phrase — *not* a search |
| `.audit/retrievals.jsonl` | The `query` text of every search, and every file path surfaced | manual (rewrite the file) | grep the log |
| `.audit/mcp-calls.jsonl` | Tool arguments, with `content`/`query` truncated to 200 chars — not removed | manual (rewrite the file) | grep the log |
| `.palinode/correction-candidates.jsonl` | The quoted span of a proposed correction | manual (rewrite the file) | grep the file |
| Remotes and clones you control | Everything above, elsewhere | manual — **replace the repository**, then re-clone | `git cat-file --batch-all-objects` in each |
| Filesystem backups | Everything above, as of the snapshot | manual (expire them) | your backup tool |
| Clones and backups you do **not** control | Everything above | **unsupported** | not checkable |

Three rows carry most of the surprise. **In a git-backed store, `rm` is not erasure** —
the file's entire edit history remains recoverable until the history itself is
rewritten. **The operational logs are not memories**: they live outside git, they are
never committed by Palinode itself, and nothing in the archive/forget path touches them.

And **the index is derived, which is not the same as harmless.** Being derived means it
needs no separate sweep of its *contents* — a rebuild from clean files produces clean
rows, with no per-record surgery. It does not mean the old bytes leave on their own. A
reindex deletes rows; it does not scrub the pages they occupied, and the store runs
SQLite in WAL mode, so recently-written page images sit in `.palinode.db-wal` (with
`.palinode.db-shm` beside it) until a checkpoint folds them in. A clean shutdown
removes the sidecars, but a running daemon holds them open — so a backup, an rsync or a
crash captures all three together, and the erased text can be readable in a sidecar
after every row that held it is gone. **Only deleting the files makes the bytes go, and
only a byte-level grep can tell you they have** — a search queries the rows, which are
exactly the thing that is already gone.

> Keep `.palinode/`, `.audit/` and `.palinode.db*` in the store's `.gitignore`.
> Palinode only ever commits the files a mutation named, so they stay untracked on
> their own — but one operator `git add -A` sweeps them into history, and then purging
> a log becomes a history rewrite.

## The erasure procedure

Destructive by design, and mostly manual. Read [what Palinode cannot
erase](#what-palinode-cannot-erase) before you start, and do step 1 before step 4 —
afterwards there is nothing left to look at.

1. **Identify the blast radius.** Search for the subject across memories, entities, and
   aliases. Personal data rarely lives in one file: check entity references, wikilinks,
   and memories that *quote* the affected content (`sources[].quote` spans in other
   files restate source text verbatim). Note the `-history.md` siblings too.
2. **Delete or redact the files.** Whole-file removal where the memory is about the
   subject; in-place redaction where a file merely mentions them. Commit the result.
3. **Write the tombstone.** A small record of the erasure act — date, scope description,
   request reference — with none of the erased content and none of the revealing
   identifiers (no subject slug, no path). This is what keeps the audit property
   honest: the ledger shows *that* something was removed without preserving what.
4. **Rewrite history — blobs *and* commit messages.** Use `git-filter-repo` (or
   `git filter-branch`) to remove the affected paths and content from all commits, and
   **a message filter as well**: Palinode writes the memory's path into every commit
   subject it makes, so a path-only rewrite leaves a history that no longer contains the
   record but still says who it was about. Then let go of the old objects — they survive
   the rewrite until the reflog and the `refs/original` backup do:

   ```bash
   rm -rf .git/refs/original
   git reflog expire --expire=now --all
   git gc --prune=now
   ```

   This changes every subsequent commit hash, which is precisely why it is the correct
   tool: erasure *should* be loud in an audit-grade system, not quiet.
5. **Replace remotes, then re-clone.** A force-push moves refs and leaves every old
   object in the remote's object database, reachable by hash and copied wholesale into
   the next clone made over a local path. So **replace** the remote repository rather
   than force-pushing to it, and re-clone every working copy you control afterwards.
6. **Delete the index, then rebuild it.** Stop the watcher and the API first so nothing
   holds the database open, then remove the database *and its sidecars* — the main file
   alone is not the index:

   ```bash
   rm -f "$PALINODE_DIR"/.palinode.db "$PALINODE_DIR"/.palinode.db-wal \
         "$PALINODE_DIR"/.palinode.db-shm
   ```

   Then let the watcher (or `palinode reindex`) rebuild from the now-clean files. Never
   edit the index instead of the files; it is derived state, so its *contents* need no
   per-record sweep — but see the note above for why deletion, not reindexing, is what
   removes the old bytes.
7. **Purge logs and expire backups.** `.audit/retrievals.jsonl`,
   `.audit/mcp-calls.jsonl` and `.palinode/correction-candidates.jsonl` all restate
   memory content and must be rewritten without the affected lines. Backups either get
   the rewritten history or an expiry date.

**Verify by absence, not by "the command ran."** The check that means something is a
sweep for a distinctive phrase across the working tree, the raw database files, every
git object (reachable or not), and every commit message:

```bash
grep -rI "<phrase>" . --exclude-dir=.git
grep -a "<phrase>" .palinode.db .palinode.db-wal .palinode.db-shm 2>/dev/null
git cat-file --batch-all-objects --batch-check='%(objectname) %(objecttype)' \
  | awk '$2=="blob" || $2=="commit" {print $1}' \
  | while read -r o; do git cat-file -p "$o" | grep -l "<phrase>" >/dev/null && echo "$o"; done
git log --all --format='%H %s%n%b' | grep "<phrase>"
```

Two of these are easy to get wrong. A plain `grep` of `.git` is not a check: packed
objects are compressed, so it reports a comforting false negative. And searching the
store — `palinode search "<phrase>"` — is not a check on the index either: it queries
rows that the rebuild already dropped, and cannot see a freed page or a `-wal` frame.
Grep the files as bytes (`grep -a`, since they are binary).

## What Palinode cannot erase

Palinode's erasure power ends at the store you operate. Outside it, these are
**unsupported** — not "hard", not "on the roadmap", but outside the software's reach,
and they must be handled by whoever does control them:

- **Provider and client conversation history.** The transcript held by the model
  provider, and the local session logs your harness keeps, are not Palinode's files.
  Memory content that came from a conversation is still in that conversation.
- **Context already supplied to an agent.** A memory delivered into a running session's
  context has already been read. Retiring or erasing it afterwards changes what will be
  supplied next time and nothing about the turn that already happened.
- **Exports a recipient holds.** Anything read out through search, `read`, the UI, an
  Obsidian vault sync or a copy-paste is a copy you no longer control.
- **Backups and clones you do not control.** The same limitation every distributed VCS
  deployment has. The mitigation is scoping (below), not tooling.

Say this out loud in any report you produce: **local retirement is not deletion
everywhere, and local cleanup is not deletion everywhere.** List what is still
outstanding rather than reporting a completed sweep you cannot vouch for.

## Previewing a change before it lands

`archive`, `restore`, `unretract` and `forget-withdraw` apply when you run them — that
default is unchanged — and each takes `--dry-run` (`dry_run: true` on the API, MCP and
the plugin client). A dry run writes nothing: no file, no history line, no index row,
no commit. It shows the record, the frontmatter delta, the relation it would record or
remove (`superseded_by`, a `retracted_prefs` entry), the retained copies above, and the
recovery path. For `forget-withdraw` it shows each step — the memories it would
restore, the spans it would un-strike, the request records it would archive — using
each step's own dry run.

```bash
palinode archive decisions/old-route.md --superseded-by decisions/new-route.md --dry-run
palinode forget-withdraw insights/forget-sneakers.md --dry-run
```

`archive-expired`, `consolidate` and `rollback` already previewed; `rollback` is the one
that previews *by default*.

## Recovery, and what it is incompatible with

- **`palinode restore`** undoes a retirement. It is the supported undo, it is visible
  (`restored_at`, `restored_from`, a history line, one commit), and it re-checks the
  memory's own `backed_by` sources on the way back.
- **`palinode forget-withdraw`** undoes a whole forget request: restores what it
  archived, un-strikes what it retracted, and retires the request record. It is a
  composition of ordinary operations, so a step can fail on its own; when one does the
  result reports `status: partial` and names what is still retired. Treat anything
  other than `withdrawn` as unfinished work.
- **Restoring a filesystem backup taken *after* a retirement** does not resurrect the
  retired claim: retirement is frontmatter inside the file, and the file is what the
  backup holds.
- **Restoring a backup taken *before* an erasure undoes the erasure.** It is the same
  filesystem operation either way; the snapshot *is* a copy of the erased data. This is
  why step 7 says to expire backups, and it is the one recovery operation that is
  flatly incompatible with a completed erasure.
- **`palinode rollback` can resurrect a retired claim.** It is a git-level revert, and
  rolling back across the commit that archived, superseded or retracted a memory
  reverts that retirement with everything else: the `status: archived` /
  `superseded_by` lines are removed, not set back to `active`, so the memory returns
  *unmarked* and is presented as current again. So rollback asks for acknowledgement:
  - the **preview** (`rollback` is `--dry-run` by default) names every retirement the
    target range would undo: the record, the relation (`status:archived`,
    `superseded_by: <ref>`, a retracted mention, a fact retired in place) and the
    commit that retired it;
  - **applying** such a rollback is **refused** and writes nothing unless you pass
    `--undo-retirements` (`undo_retirements=true` on the API and MCP tool). The
    refusal says why and points at `palinode restore` or `palinode corrections undo`,
    which bring a record back *visibly* (`restored_at`, a history line);
  - an **acknowledged** rollback reports `status: undid_retirements` and names every
    record it resurrected. It never reports plain success, and it re-indexes the file
    at once so search, resolve and priming agree that the record is current.
- **`palinode rollback` cannot recover a record whose history was rewritten** — there is
  nothing left to read back — and it reports the file as not found rather than failing
  silently.

An interrupted mutation is an error, not a quiet partial success: an archive whose index
push fails leaves the file marked and the index unmarked, reports a failure, and is
reconciled by a rebuild from the files — which are the source of truth.

## Alternatives, and why they are not the default

- **Redaction-in-place with history rewrite of only the affected hunks** — smaller
  blast radius, same tooling, more per-case effort. Appropriate when a file is mostly
  about something else.
- **Scoped crypto-shredding** — encrypt per-subject content under a per-subject key and
  erase by destroying the key. This is the architecture that reconciles immutable
  history with erasure *without* rewriting it, and it is the direction the field's
  standards work points. Palinode does **not** implement it today; it is on the
  roadmap's horizon, not in the product. This page will say so plainly until that
  changes.
- **"We never delete" is not an answer.** It is an admirable property of a
  contradiction model and a non-answer to a legal obligation. The two coexist by being
  different operations (see above).

## What this costs you as an operator

A history rewrite invalidates commit hashes that anything external may reference, and
it requires coordination across every mirror. That is the real price of erasure in a
git-backed store, and it is worth knowing *before* the first request arrives. The
mitigation is scoping: one memory directory per data-subject population where
erasure-heavy use is expected (e.g., one store per client engagement) keeps a rewrite's
blast radius to the store that needs it.

## Retention

Palinode imposes no retention schedule of its own: files persist until removed, and
git history persists until rewritten. If your obligations include maximum retention
periods, implement them at the file layer (dated reviews of `daily/`, archive sweeps) —
the mechanisms above are the enforcement path, and the tombstone convention gives the
schedule an auditable record.

One thing those sweeps deliberately do **not** cover: identity documents. Age-based
retirement — the TTL sweep's `expires_at`, and a staleness `ARCHIVE` proposed by
consolidation — applies to episodic content (daily notes, insights, research, status
documents) and is refused on files about a person or a long-lived entity (`people/`,
project profile documents, anything declaring `update_policy: replace` or `core: true`).
Those are retired by supersession or retraction — a statement that the fact changed or
was never true — because "this fact is old" is not evidence that it stopped being true.
A document can state its own regime with `retirement_policy: age-eligible |
superseded-only`. **This is orthogonal to erasure:** a retention schedule that must
reach personal data uses the erasure procedure above, which is destructive by design;
the retirement policy governs only what ages out of recall on its own.
