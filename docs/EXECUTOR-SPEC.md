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
}
```

The executor dispatch handles these AI consolidation operations:

- `KEEP`
- `UPDATE`
- `MERGE`
- `SUPERSEDE`
- `ARCHIVE`
- `RETRACT`

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
| Required fields | Non-empty `ids`, `new_text`. |
| Preconditions | `ids[0]` matches a list-item fact in current content. If `nightly_policy=True`, every source ID must match a fact whose text starts with `[YYYY-MM-DD]`, and all extracted dates must be identical. |
| Postconditions | The first source fact is updated with normalized `new_text`, then its marker is rewritten from `<!-- fact:<ids[0]> -->` to `<!-- fact:merged-<ids[0]> -->`. For each remaining source ID, every matching single-line list item is removed. |
| Source removal | Source facts after the first ID are removed from the main file. The first source remains as the merged fact with the `merged-` ID. If a remaining source ID appears on multiple matching list-item lines, all matching lines are removed. |
| History | None. |
| Stats | `merged += 1` only if content changed. With nightly rejection, `merge_rejected += 1`. |
| Failure behavior | Missing or empty `ids`, missing or empty `new_text`, or no match for `ids[0]` produces a silent no-op. Source IDs after the first that do not match are ignored. |

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
| Required fields | `id`. Optional `rationale` or `reason`; `rationale` wins when both are present. |
| Preconditions | A list-item line containing `<!-- fact:<id> -->` exists, and the file is not protected by `update_policy: replace`. |
| Postconditions | Every matching single-line fact item is removed from the main file. |
| Source removal | Matching source facts are removed from the main file. If the same fact ID appears on multiple matching list-item lines, all matching lines are removed. |
| History | Before removal, the first matching line's text is appended as `Archived: <old_text> (reason: <reason>) <!-- fact:<id> -->` to the corresponding history file. |
| Stats | `archived += 1` only if content changed. With replace-guard rejection, `protected_rejected += 1`. |
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

If `op` is present but does not match one of the six handled AI operations, the
executor silently skips it with no mutation and no stat increment.

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
| `MERGE` | Not generally re-runnable. The first run rewrites `ids[0]` to `merged-<ids[0]>`, so a second run using the original IDs usually cannot match the first source ID and becomes a silent no-op. Remaining source facts may already be removed. |
| `SUPERSEDE` | Not a pure no-op. The original fact ID remains on the tombstoned line, so the same operation can match it again, wrap the already tombstoned text again, insert another `supersedes-<id>` line, append history again, and increment `superseded` again. |
| `ARCHIVE` | Usually becomes a no-op after the first run because the source line is removed. |
| `RETRACT` | Not a pure no-op. The original fact ID remains on the tombstoned line, so the same operation can tombstone the already tombstoned text again, append history again, and increment `retracted` again. |

## Write, History, and Git Semantics

`apply_operations` writes the main file through `_atomic_write_text` after all
operations have been processed. The write happens even if content is unchanged.

Atomic writes use a temporary file in the target directory, preserve the
existing file mode when the target already exists, flush and fsync the temporary
file, replace the target via `os.replace`, and fsync the target directory. On
write failure, the temporary file is removed when possible and the exception is
raised.

`ARCHIVE`, `SUPERSEDE`, and `RETRACT` preserve history in a sibling history
file. The history path is derived by stripping a trailing `-status.md` or `.md`
from `file_path` and appending `-history.md`.

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

`apply_operations` does not create git commits. Git commit behavior belongs to
caller-layer paths such as the consolidation runner and write-time dedup flow.
The returned stats dict is the executor's auditable record of what happened
inside this call.

## Unknown Operation Handling

`apply_operations` dispatch handles only the six operations listed in this
spec. Explicit unknown operation strings are skipped and counted as skipped;
malformed operation objects are ignored by the parser before dispatch.
