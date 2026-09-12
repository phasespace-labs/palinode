# Consolidation Executor Determinism Specification

This document is the public behavioral contract for
`palinode.consolidation.executor.apply_operations(file_path, operations,
nightly_policy=False)`. It describes the executor as it behaves in this
checkout, so tests and future ports can assert this contract without
reverse-engineering implementation details.

The source of truth for memory remains markdown files on disk. SQLite search
indexes are derived state. Related private derivation notes are intentionally
not quoted or linked from this public spec.

## Scope

`apply_operations` reads one markdown file, applies an ordered list of proposed
operation dicts, writes the final content back to that same file, and returns a
stats dict:

```python
{
    "kept": 0,
    "updated": 0,
    "merged": 0,
    "superseded": 0,
    "archived": 0,
    "retracted": 0,
    "merge_rejected": 0,
    "protected_rejected": 0,
    "contradicts_proposed": 0,
    "unmatched": 0,
    "review_flagged": 0,
}
```

The executor dispatch handles these AI consolidation operations:

- `KEEP`
- `UPDATE`
- `MERGE`
- `SUPERSEDE`
- `ARCHIVE`
- `RETRACT`
- `PROPOSE_CONTRADICTS`

Explicit operation strings outside that set have no dispatch branch and are
silently skipped.

## Determinism Model

For the same starting file content and the same ordered operations list,
`apply_operations` produces the same final main-file content, except for
date-bearing markers and history entries that use current UTC time.

Operations apply sequentially against a mutated in-memory `content` string.
Order is part of the contract: a later operation sees the result of earlier
operations in the same call.

Each operation item must be a dict. Non-dict items are rejected with a warning,
do not mutate content, and do not increment any stat. A dict with no `op` key is
treated as `KEEP`. Operation names are uppercased before dispatch.

Missing required fields are skipped silently and do not increment stats, except
for the two guarded rejection cases documented below.

The final main file is written through `_atomic_write_text` after the loop even
when no operation changed the content.

## Fact Matching

All operation helpers match markdown list items containing an HTML fact marker:

```markdown
- Fact text <!-- fact:fact-id -->
* Fact text <!-- fact:fact-id -->
```

The regexes are line-oriented, accept leading whitespace, accept `-` or `*`
list markers, and match the first matching fact unless stated otherwise. Fact
IDs are escaped before interpolation into regexes.

`new_text` is normalized before insertion by trimming surrounding whitespace and
removing one leading `- ` or `* ` list marker if present.

## Operation Contracts

### KEEP

| Field | Contract |
| --- | --- |
| Preconditions | `op` is absent or uppercases to `KEEP`. |
| Postconditions | Main-file content is unchanged. |
| Stats | `kept += 1`. |
| Failure behavior | None. |

### UPDATE

| Field | Contract |
| --- | --- |
| Required fields | `id`, `new_text`. |
| Preconditions | A list-item line containing `<!-- fact:<id> -->` exists. |
| Postconditions | The first matching line's text is replaced with normalized `new_text`; the original fact ID is preserved. The replacement line is `same-list-prefix + normalized-new-text + " <!-- fact:<id> -->"`. |
| Source removal | None. |
| History | None. |
| Stats | `updated += 1` only if content changed. |
| Failure behavior | Missing `id`, missing or empty `new_text`, no matching fact ID, or replacement text identical to current content produces a silent no-op with no stat increment. |

### MERGE

| Field | Contract |
| --- | --- |
| Required fields | Non-empty `ids`, `new_text`. Optional `rationale` or `reason`; `rationale` wins when both are present. |
| Preconditions | `ids[0]` matches a list-item fact in current content. If `nightly_policy=True`, every source ID must match a fact whose text starts with `[YYYY-MM-DD]`, and all extracted dates must be identical. |
| Postconditions | The first source fact is updated with normalized `new_text`, then its marker is rewritten from `<!-- fact:<ids[0]> -->` to `<!-- fact:merged-<ids[0]> -->`. For each remaining source ID, every matching single-line list item is removed. |
| Identical text | When normalized `new_text` equals `ids[0]`'s current text, the merge still runs: `ids[0]`'s line is left byte-identical (text **and** ID — nothing is rewritten, so nothing is renamed) and `ids[1:]` are retired as above, recorded as merged into `<ids[0]>`. A source ID in `ids[1:]` equal to `ids[0]` is skipped in this case, since with no `merged-` rename its pattern would also match the surviving line. `merged += 1` iff at least one `ids[1:]` line was removed; if there is nothing left to retire the whole op is an unmatched no-op. |
| Source removal | Source facts after the first ID are removed from the main file. The first source remains as the merged fact with the `merged-` ID. If a remaining source ID appears on multiple matching list-item lines, all matching lines are removed. |
| History | Before mutation, every source fact's original text is appended verbatim to the corresponding history file — one entry per retired line, tagged with that source's own fact ID: `Merged into merged-<ids[0]> (YYYY-MM-DD): <old_text> (reason: <reason>) <!-- fact:<source-id> -->`. `ids[0]` is recorded with its pre-merge text (its text is replaced in place); each removed `ids[1:]` line is recorded (a duplicate-ID line is recorded once per line). In the identical-text case `ids[0]` is not recorded — its text does not change — and the entries name `<ids[0]>` as the merge target instead of `merged-<ids[0]>`. Source IDs that match nothing produce no entry. A no-op MERGE writes no history. |
| Stats | `merged += 1` only if content changed. With nightly rejection, `merge_rejected += 1`. |
| Failure behavior | Missing or empty `ids`, missing or empty `new_text`, or no match for `ids[0]` produces a no-op with no history append, counted as `unmatched` and logged. Source IDs after the first that do not match are ignored. |
| Outcome reporting | When the caller passes the optional `applied_merges` list, the index into `operations` of every MERGE that was applied is appended to it. The consolidation runner uses this to log a `[MERGE]` line in a status document's Consolidation Log only for merges that happened — a dropped, unmatched, or nightly-rejected MERGE leaves no line. |

### SUPERSEDE

| Field | Contract |
| --- | --- |
| Required fields | `id`, `new_text`. Optional `reason`. |
| Preconditions | A list-item line containing `<!-- fact:<id> -->` exists, and the file is not protected by `update_policy: replace`. |
| Postconditions | The first matching fact text is wrapped in strikethrough, followed by `[superseded YYYY-MM-DD]`, preserving the original fact marker. A new following list item is inserted with normalized `new_text` and fact ID `supersedes-<id>`. |
| Exact marker shape | `~~<old_text>~~ [superseded YYYY-MM-DD] <!-- fact:<id> -->` then a newline and `<same-list-prefix><new_text> <!-- fact:supersedes-<id> -->`. |
| Source removal | The original fact is not removed; it remains tombstoned in place. |
| History | Appends `Superseded (YYYY-MM-DD): <reason> <!-- fact:<id> -->` to the corresponding history file. |
| Stats | `superseded += 1` only if content changed. With replace-guard rejection, `protected_rejected += 1`. |
| Failure behavior | Missing `id`, missing or empty `new_text`, or no matching fact ID produces a silent no-op. |

### ARCHIVE

| Field | Contract |
| --- | --- |
| Required fields | `id`. Optional `rationale` or `reason`; `rationale` wins when both are present. Optional `superseded_by`, read only by the retirement-policy guard below. |
| Preconditions | A list-item line containing `<!-- fact:<id> -->` exists, the file is not protected by `update_policy: replace`, and either the file's retirement policy is `age-eligible` or the operation carries a non-empty `superseded_by`. |
| Postconditions | Every matching single-line fact item is removed from the main file. |
| Source removal | Matching source facts are removed from the main file. If the same fact ID appears on multiple matching list-item lines, all matching lines are removed. |
| History | Before removal, the first matching line's text is appended as `Archived: <old_text> (reason: <reason>) <!-- fact:<id> -->` to the corresponding history file. `superseded_by` is not written to the history entry. |
| Stats | `archived += 1` only if content changed. With replace-guard or retirement-policy rejection, `protected_rejected += 1`. |
| Failure behavior | Missing `id` or no matching fact ID produces a silent no-op. |

### RETRACT

| Field | Contract |
| --- | --- |
| Required fields | `id`. Optional `reason` or `rationale`; `reason` wins when both are present. |
| Preconditions | A list-item line containing `<!-- fact:<id> -->` exists, and the file is not protected by `update_policy: replace`. |
| Postconditions | The first matching fact text is wrapped in strikethrough and followed by `[RETRACTED YYYY-MM-DD]` or `[RETRACTED YYYY-MM-DD — <reason>]`, preserving the original fact marker. |
| Source removal | None. The tombstone remains visible in the main file. |
| History | Appends `Retracted (YYYY-MM-DD): <reason> <!-- fact:<id> -->` to the corresponding history file. |
| Stats | `retracted += 1` only if content changed. With replace-guard rejection, `protected_rejected += 1`. |
| Failure behavior | Missing `id` or no matching fact ID produces a silent no-op. |

### PROPOSE_CONTRADICTS

Records a typed `contradicts` link in the target file's frontmatter. It is the
no-winner counterpart to `SUPERSEDE`: two memories disagree, neither is retired,
and the conflict is surfaced for review (`lint` `open_contradictions`, `trace`,
and the search-result marker). `SUPERSEDE` remains the only winner-picking op.

| Field | Contract |
| --- | --- |
| Required fields | The refs to link, read from `contradicts`, else `refs`, else `ids` — first present key wins. A single string is accepted and coerced to a one-element list. Any other field (`id`, `rationale`) is ignored by the executor; the runner's consolidation log reads them. |
| Ref format | Each ref is a `category/slug` memory identity, validated by `palinode.core.typed_links.normalize_link_refs`: non-empty string, no `..`, no leading `/`, no newline. Duplicates are dropped, order preserved. |
| Preconditions | None on the body. **Not** subject to the `update_policy: replace` guard — recording a conflict forks nothing into history. |
| Postconditions | The normalized refs are merged into the frontmatter `contradicts:` list (idempotent — refs already present are not duplicated). The body and every other frontmatter field are preserved verbatim. Later operations in the same call operate on the re-split body. |
| Source removal | None. Nothing is retired, tombstoned, or moved to history. |
| History | None. No `-history.md` write and no dependency propagation — `contradicts` is an association edge, not an extension edge. |
| Stats | `contradicts_proposed += 1` only if the frontmatter changed. |
| Failure behavior | A malformed ref list is rejected as a whole with a warning (`PROPOSE_CONTRADICTS rejected (malformed refs)`), no stat increment, no mutation. Missing/empty refs are a silent no-op. |

## Validation and Rejection Rules

### Malformed Operations

An operation item that is not a dict is skipped with a warning:

```text
Malformed operation (expected dict, got <type>): <value>
```

No stats are incremented.

### Missing or Unknown Operation Type

If `op` is absent, the executor treats the operation as `KEEP` and increments
`kept`.

If `op` is present but does not match one of the seven handled AI operations,
the executor silently skips it with no mutation and no stat increment.

### Missing Required Fields

Operations with missing or empty required fields are skipped silently. They do
not increment success stats or rejection stats.

### `update_policy: replace` Guard

Before applying operations, the executor parses the target file frontmatter
through the shared markdown parser. If parsed metadata contains
`update_policy: replace`, the document is treated as a living current-state
document. `SUPERSEDE`, `ARCHIVE`, and `RETRACT` are rejected before helper
execution.

For each guarded rejection:

- content is unchanged;
- a warning is logged;
- `protected_rejected += 1`;
- no operation-specific success stat increments.

`KEEP`, `UPDATE`, and `MERGE` are allowed on replace-policy documents.

If parsing fails or parsed metadata does not contain `update_policy: replace`,
the guard falls open and does not protect the file. If the raw text contains
`update_policy: replace` but parsed metadata does not, the executor logs a
warning about possible frontmatter corruption and still falls open.

### Retirement Policy Guard

Eligibility for **age-based** retirement is a property of the document, not of
the fact's age. Before applying operations, the executor classifies the target
document from its own path and frontmatter into one of two regimes:

- `age-eligible` — episodic documents: daily notes, insights, research, status
  documents (`projects/<slug>-status.md`), decisions, inbox items, and anything
  unclassified. This is the default and the pre-existing behavior.
- `superseded-only` — identity / profile documents: files under `people/`, a
  project's profile document (`projects/<slug>.md`, as distinct from its
  `-status.md` layer), `type: PersonMemory`, `category: person`,
  `update_policy: replace`, and `core: true`.

A document may declare its own regime with a `retirement_policy:` frontmatter
field, whose value is `age-eligible` or `superseded-only`. The declaration wins
over every inferred signal, in both directions; an unrecognized value is logged
and ignored, and classification falls back to the inferred regime.

On a `superseded-only` document, `ARCHIVE` is rejected unless the operation
carries a non-empty `superseded_by`. That field is the whole distinction between
the two retirement arguments the executor can tell apart deterministically:
naming a successor states a supersession, while its absence leaves age or
staleness as the only argument, and age is not a retirement reason for these
documents. The rationale text is never parsed; it is logged with the rejection.

For each rejection:

- content is unchanged;
- a warning is logged naming the signal that classified the document and the
  operation's rationale;
- `protected_rejected += 1`;
- `archived` does not increment.

`SUPERSEDE` and `RETRACT` are not affected by this guard — both state a reason
that is not age — and neither are `KEEP`, `UPDATE`, `MERGE`, or
`PROPOSE_CONTRADICTS`. Whole-file retirement outside the executor (the on-demand
archive path, which requires an explicit reason from a caller who is not the
compaction model) is likewise untouched. If frontmatter parsing fails, the guard
falls open to `age-eligible`, for the same reason the replace guard falls open:
a malformed file must never block consolidation.

The same classification governs the TTL sweep, which skips `superseded-only`
documents whose `expires_at` has passed rather than archiving them.

### Nightly MERGE Guard

When `nightly_policy=True`, each `MERGE` with non-empty `ids` and `new_text` is
checked before mutation. Every source fact must be found with a leading
`[YYYY-MM-DD]` date tag at the start of the fact text, and all extracted dates
must be the same calendar date.

If any source fact is undated, missing, or cross-date, the merge is rejected:

- content is unchanged;
- a warning is logged;
- `merge_rejected += 1`;
- `merged` does not increment.

When `nightly_policy=False`, this guard is not applied.

## Idempotency Matrix

| Operation reapplied to its own result | Behavior |
| --- | --- |
| `KEEP` | Stable; increments `kept` each time. |
| `UPDATE` | Stable when reapplying the same replacement to the same retained ID. If the second replacement would not change content, `updated` does not increment on that second run. |
| `MERGE` | Not generally re-runnable. The first run rewrites `ids[0]` to `merged-<ids[0]>`, so a second run using the original IDs usually cannot match the first source ID and becomes a no-op with no history append. Remaining source facts may already be removed. The identical-text case keeps `ids[0]`'s ID, so a second run matches it again but finds no `ids[1:]` lines left to retire: content is unchanged, no history is appended, `merged` does not increment. |
| `SUPERSEDE` | Not a pure no-op. The original fact ID remains on the tombstoned line, so the same operation can match it again, wrap the already tombstoned text again, insert another `supersedes-<id>` line, append history again, and increment `superseded` again. |
| `ARCHIVE` | Usually becomes a no-op after the first run because the source line is removed. |
| `RETRACT` | Not a pure no-op. The original fact ID remains on the tombstoned line, so the same operation can tombstone the already tombstoned text again, append history again, and increment `retracted` again. |
| `PROPOSE_CONTRADICTS` | Idempotent. A ref already in the `contradicts:` list is not duplicated, the content is unchanged, and `contradicts_proposed` does not increment on the second run. |

## Write, History, and Git Semantics

`apply_operations` writes the main file through `_atomic_write_text` after all
operations have been processed. The write happens even if content is unchanged.

Atomic writes use a temporary file in the target directory, preserve the
existing file mode when the target already exists, flush and fsync the temporary
file, replace the target via `os.replace`, and fsync the target directory. On
write failure, the temporary file is removed when possible and the exception is
raised.

`MERGE`, `ARCHIVE`, `SUPERSEDE`, and `RETRACT` preserve history in a sibling
history file — every op that retires a fact's current text writes to the
sibling, so the main file plus its history sibling are together lossless
without recourse to git. The history path is derived by stripping a trailing
`-status.md` or `.md` from `file_path` and appending `-history.md`.

History entries are timestamped with current UTC to minute precision:

```markdown
- [YYYY-MM-DD HH:MM] <entry text> <!-- fact:<id> -->
```

New history files are created with:

```yaml
---
category: history
core: false
status: archived
---
```

Existing history files are passed through `_ensure_archived_frontmatter` before
append. If they have no frontmatter, archived frontmatter is prepended. If they
have frontmatter but no `status:` field, `status: archived` is injected. If
they already have any `status:` field, that explicit status is preserved.

`apply_operations` does not create git commits for the target file or its
history sibling. Git commit behavior for those belongs to caller-layer paths
such as the consolidation runner and write-time dedup flow. The one exception
is dependency propagation (next section), whose writes land in other files and
are committed by the propagation step itself. The returned stats dict is the
executor's auditable record of what happened inside this call.

## Dependency Propagation (`backed_by`)

Typed links carry two different propagation semantics. `backed_by` is an
extension edge — a dependent's claim rests on its source, so retiring the
source must reach the dependent. `contradicts` is an association edge — two
memories disagree, neither wins — and is surfaced by `lint`, never propagated.

After the main file is written, if any `SUPERSEDE`, `ARCHIVE`, `RETRACT` or
`MERGE` in the call changed content, the executor flags every dependent of the
target file (`palinode.consolidation.propagate.flag_dependents`):

| Field | Contract |
| --- | --- |
| Trigger | At least one retiring op (`SUPERSEDE` / `ARCHIVE` / `RETRACT` / `MERGE`) incremented its stat in this call. A rejected, unmatched, or dropped op does not trigger propagation. |
| Source ref | The target file's memory-dir-relative path without `.md` (`project/foo` for `project/foo.md`). A `-status.md` layer is matched and recorded under its base ref (`project/foo` for `project/foo-status.md`), the identity the history writer uses. A target outside the memory dir has no dependents. |
| Dependents | Every `.md` under the memory dir whose frontmatter `backed_by` names the source ref (with or without `.md`), excluding the source itself, `-history.md` siblings, skip-dir files (`archive/`, `logs/`, `daily/`, `inbox/`, `prompts/`, `.obsidian/`), unreadable frontmatter, and `status: archived` memories. Scanned in sorted path order. |
| Write | One `stale_backing` entry is appended to the dependent's frontmatter list: `{ref: <source ref>, op: <kinds>, at: <UTC ISO-8601 seconds>, facts: [<retired ids>], reason: <joined reasons>}`. `op` is the retirement kinds that fired, lowercase, joined by `, ` in the order supersede, archive, retract, merge. `facts` and `reason` are omitted when empty. The body and every other frontmatter field are preserved; `status` is never changed. |
| Idempotency | Keyed on `ref`: a dependent already carrying an entry for this source is not written, not re-indexed, not committed. Re-applying the same ops therefore flags nothing on the second run. |
| Hops | One. Dependents of dependents are not walked. |
| Index | Each written dependent is re-indexed through the frontmatter-only path (no re-embed) so the flag appears in search-result metadata. |
| Git | All written dependents are staged and committed in one commit, `<prefix> backed_by review: <source ref> <kinds> -> N dependent(s)`, when `git.auto_commit` is on. |
| Stats | `review_flagged` = number of dependents newly written in this call. |
| Failure behavior | Best-effort per dependent: a dependent that cannot be read or written is logged and skipped; propagation never fails the retirement that triggered it. |
| Clearing | Re-saving the dependent through any save surface rebuilds its frontmatter and drops the flag. No executor op clears it. |

The on-demand `archive_memory` (archive / supersede of a whole file) and
`retract_mentions` (strand-level retract) paths apply the same rule after
their own commit, with `op` = `archive` / `supersede` / `retract`, and report
the flagged rel paths as `review_flagged` in their result dicts. The TTL sweep
does not propagate. Because archived dependents are skipped, `restore_memory`
re-runs the one-hop check from the other side: on restore, each of the restored
memory's own `backed_by` refs whose target is no longer active (`status:
archived`, with or without `superseded_by`, or no file at `<ref>.md` /
`<ref>-status.md`) gains an entry with `op` = `restore-check` and a `reason`
naming the observed state, keyed on `ref` like every other entry, written and
committed in the restore itself and reported as `stale_backing` in its result.

## Proposers and the `lint` Actor

`apply_operations` does not know or care who proposed an operation. Two
proposers reach it today, and the difference is recorded outside the executor,
in the provenance its callers write.

The LLM proposer is the consolidation runner's compaction pass. The second is
deterministic: `palinode lint --propose` maps lint findings onto operations —
one finding class per operation, no wording invented — and `--apply` hands the
applicable ones to the existing callers with `source="lint"`. There is no new
write path. A whole-document retirement goes through the on-demand
`archive_memory`, which is already specified above; an operation addressed to a
file's content goes through `runner.apply_proposed_operations`, which performs
the same pre-apply fact-id capture, `apply_operations` call, status-document log
entry and one-mutation-one-commit staging as the weekly pass.

What the actor changes is the audit trail, in the two durable places:

| Surface | With a `lint` actor |
| --- | --- |
| History sibling | The entry gains a trailing `[actor: lint]`, after the reason. |
| Commit subject | `archive_memory` appends `(actor: lint)`; `runner.apply_proposed_operations` writes `<prefix> lint-proposed ops: <kinds>`. |
| Status document | The `## Consolidation Log` line carries the proposal's rationale, which begins `lint:`. Written only when the target is a `-status.md` document. |

Absent an actor — a direct CLI, API or MCP call — nothing changes; the messages
are byte-identical to what they were.

The deterministic proposer emits a deliberately small vocabulary: whole-document
`ARCHIVE`, `PROPOSE_CONTRADICTS`, and the one `UPDATE` described below.
Operations that require *invented* replacement text (`MERGE`, `SUPERSEDE`, and
any other `UPDATE`) are not proposed by it, because choosing that text is
judgement; those findings are emitted as advisory `PROPOSE_*` notes for a human
and are never applied. Age-based `ARCHIVE` is additionally restricted by
document class (ADR-020) and honours `consolidation.allowed_ops`.

That document-class restriction is two layers, and the deterministic proposer
keeps them separate. The first is the ADR-020 invariant: the proposer calls the
same classifier the Retirement Policy Guard above reads, so a `superseded-only`
document is never nominated for an age-argued `ARCHIVE` and the proposer and the
executor cannot drift on what an identity document is. The second is
proposal-side conservatism — a short local list of classes the proposer declines
to nominate even though the guard would accept them, which today holds
`decisions/` alone (ADR-020 calls that regime conservative, not forbidden). A
status document, `projects/<slug>-status.md`, is age-eligible in both layers and
is proposed. Every skipped finding records which layer stopped it:
`retirement_policy: superseded-only (<signal>)` names the invariant and the
signal that classified the document, `proposer: conservative class` names the
preference. The proposer may be stricter than the guard; it must never be looser,
since a proposal the guard is obliged to reject is a proposer bug rather than a
policy difference.

The exception is a relative date, where the replacement text is arithmetic and
not judgement: "yesterday" in a memory whose `created_at` is 2026-09-10 means
2026-09-09, and the lint pass computes it with the same normaliser that runs at
write time. A `relative_dates` finding on a list-item fact therefore maps to an
ordinary `UPDATE` — `id` is the fact's own id, `new_text` is the fact's text
with the phrase replaced — carrying the phrase and the anchor in its rationale.
The contract above is unchanged: the executor sees a normal `UPDATE` and neither
knows nor cares that lint proposed it. Findings that resolve to no date (vague
phrases, intervals, an undated memory), and findings whose phrase sits in quoted
text, a blockquote or code, are recorded as skipped with the reason instead;
`UPDATE` must also be in `consolidation.allowed_ops` for the proposal to be
applicable.

## Divergence Notes

DIVERGENCE / TODO: ADR-016 and private derivation notes describe human
actor-class operations such as `CREATE`, `REVISE`, `APPROVE`, and `REJECT`, but
current `apply_operations` dispatch handles only the six AI operations listed
in this spec. Explicit unknown op strings are silently skipped.

DIVERGENCE / TODO: In this checkout,
`palinode/consolidation/op_registry.py` is not present, despite derivation
notes referencing it. Current executor behavior is the direct `if`/`elif`
dispatch described above.
