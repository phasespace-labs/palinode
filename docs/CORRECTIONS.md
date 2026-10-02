# Correcting a memory: preview, apply, recover

A memory you cannot correct is a memory you stop trusting. This page is the
short walkthrough: how to see exactly what a correction would change before it
changes anything, how to apply it so a fresh session actually reads the new
answer, and how to get back if you were wrong about being wrong.

Three things are deliberately **not** the same thing, and this page keeps them
apart throughout:

| | What it does | Where |
|---|---|---|
| **Correction** | Records a replacement. The old statement is archived, linked as history, and stays on disk and in git. | this page |
| **Retirement** | Withdraws a statement with no successor. Also kept. | this page |
| **Erasure** | Actually removes data, git history included. Destructive, separate procedure. | [DATA-LIFECYCLE.md](DATA-LIFECYCLE.md) |

If your obligation is "this must be *gone*", you are on the wrong page — read
[the data lifecycle](DATA-LIFECYCLE.md), which explains why `rm` is not erasure
in a git-backed store.

## The shape of it: two phases, and the first writes nothing

Every surface — CLI, REST API, MCP and the plugin — runs the same two-phase
contract.

**PREVIEW** reads. It returns:

- the **old text** and the **proposed new text**;
- the affected document (and, if you named one, the affected claim);
- the target's **exact source revision** — a SHA-256 over the file as it stands;
- the **rationale**, and the **source** of the correction (your own statement,
  or a mined candidate with its quoted span and session identity);
- the **project scope**;
- the **relation** that would be recorded — `supersedes` / `superseded_by`, or
  a plain retirement;
- **every other record that quotes or derives from the target**, reported and
  never rewritten;
- the exact **recovery command**;
- the `apply` invocation, with the revision baked in.

**APPLY** writes, and only with two things: the revision the preview showed and
an explicit confirmation. Apply is never the default on any surface.

It persists through the paths that already exist — the same save any other
memory goes through, and the same archive `palinode archive` performs — so the
git commit, the `{slug}-history.md` audit sibling and the index update are the
ones you already know. The commit subject names the actor as a reviewed
correction, and carries the candidate id when a mined candidate was the source.

## Walkthrough

Start from a decision you want to change:

```bash
palinode read decisions/harbor-notes-storage.md --meta
```

### 1. Preview

```bash
palinode corrections preview \
  --target decisions/harbor-notes-storage \
  --replacement 'Use the shared hosted service because concurrent writers need transactional coordination.' \
  --reason 'concurrent writers need transactional coordination' \
  --backed-by decisions/harbor-notes-concurrent-write-requirement
```

Read the `referencing_records` block before anything else — it is the blast
radius. Those records are *reported*, not changed: each has its own author and
its own reason for citing the target, and rewriting them would turn one
reviewed decision into an unreviewed cascade.

`--backed-by` names the replacement's **own** support. The record being
superseded is never cited as support for its replacement: a verified quote
establishes what a source said, never that the claim is true. If you pass no
support, the preview says so rather than quietly borrowing the old record's.

### Preserve the other claims

A document correction archives the entire original. Include its untouched
claims in `--replacement` so they remain available to recall. For example:

```bash
palinode corrections preview --target decisions/notifier \
  --replacement 'The notifier transport is Y; failed notifications go to the Q dead-letter queue.'
```

Preview reports `content_loss.removed_text`: the exact original wording absent
from the proposed replacement, including the old claim you intend to correct.
When more than one unit is absent, preview refuses and prints no apply command;
apply independently enforces the same check before writing. Keep the neighboring
claims in the replacement, or use `--claim ID` to select a fact-marked line.
If dropping all listed text is intentional, pass `--allow-content-loss` to both
preview and apply (`allow_content_loss: true` in REST, MCP and OpenClaw).
Explicit retirement already means withdrawing the target and needs no extra flag.

This guard is deterministic: it compares wording across paragraphs, list items,
sentences and semicolon-separated clauses, ignoring fact IDs, headings and
generated navigation. Soft-wrapped prose stays together; case, whitespace and
ending punctuation may differ. One omitted unit is treated as the intended
correction; two or more require review. It does not infer semantic
equivalence: paraphrasing several claims can require the flag, and multiple facts
inside one unsplit clause cannot be distinguished. No model runs in this path.
Single-claim corrections retain their existing behavior.

### 2. Apply

Copy the command the preview printed — it already carries the revision:

```bash
palinode corrections apply \
  --target decisions/harbor-notes-storage \
  --replacement 'Use the shared hosted service because concurrent writers need transactional coordination.' \
  --reason 'concurrent writers need transactional coordination' \
  --backed-by decisions/harbor-notes-concurrent-write-requirement \
  --expect-revision <the revision from the preview> --confirm
```

If the file changed in between — a consolidation pass, another agent, your own
editor — the apply is **refused** with both revisions. Re-preview and read the
new text before you confirm; it is exactly the moment where applying a stale
decision would be worst.

### 3. Verify

```bash
palinode prime --project harbor-notes
palinode search 'shared hosted service'
palinode read decisions/harbor-notes-storage.md --meta
palinode history decisions/harbor-notes-storage.md --detail full
```

The replacement is what search and the session-start digest return. The
original is `status: archived`, carries `superseded_by`, and is still readable
by name — history, not deletion.

## Retirement: withdrawing with no successor

When nothing replaces the statement:

```bash
palinode corrections preview --target decisions/harbor-notes-storage --action retire \
  --reason 'the prototype was cancelled'
```

Same two phases, same revision check. The record leaves default recall and
stays retrievable.

## What gets refused, and why

The contract refuses rather than guessing. Each refusal returns what you need
to decide:

| Refusal | What you get back |
|---|---|
| **Stale revision** — the target changed since the preview | both revisions; nothing written |
| **Ambiguous target** — a slug naming two memories | every match, with its path and title |
| **Ambiguous claim** — a claim id naming two lines (ids are derived from line text, so identical lines share one) | every matching line |
| **No target** — a mined candidate whose span never said what it replaced | nothing inferred; you name the target |
| **A living document, claim-level** — the target declares `update_policy: replace` | correct the whole document instead |
| **Capture paused** | the same refusal an explicit `palinode save` gets; `palinode controls resume --capture` |

`update_policy` is **inherited from the target**, never imposed by the
correction: a document that appends keeps appending, a living document stays
living. And an ordinary later observation never silently retires a decision —
retirement happens only through this explicit, confirmed path.

## When only half of it lands

A document-level supersession is two writes — save the replacement, retire the
original — and nothing wraps them in a transaction. If the second one fails,
the result is not a refusal and not an error: it is `applied: "partial"`, and
it names both halves.

```
written      decisions/harbor-notes-storage-corrected.md is saved, active, and
             carries supersedes: decisions/harbor-notes-storage
not written  decisions/harbor-notes-storage.md is still current — no
             status: archived, no superseded_by, still returned by default
             recall. The store now serves both statements
```

It also carries the one command that finishes the job:

```bash
palinode archive decisions/harbor-notes-storage.md \
  --superseded-by decisions/harbor-notes-storage-corrected.md --reason '…'
```

Every surface reports it: the CLI exits non-zero, `POST /corrections/apply`
returns **409 with the payload** (the same code as a refusal, told apart by
`applied: "partial"` — a refusal carries `error`), and the MCP and plugin tools
lead with *partially applied* rather than *refused*.

**Undo is not the way out of this state.** `palinode corrections undo` restores
an *archived* original, and here nothing was archived — it refuses a record
whose status is still active. To unwind instead of completing, retire the
replacement (`palinode archive <replacement>`); that is a second decision, with
its own record.

The order of the two writes is deliberate. Saving first means the failure mode
is two current records, one of which says it supersedes the other: visible,
readable, and nothing has left recall. Archiving first would mean the original
is retired with `superseded_by` pointing at a record that does not exist — the
store would be missing the answer entirely, and nobody would notice.

A claim-level correction has no partial of this shape: it is one executor
operation over one file, written in a single atomic write. If the git commit
after it fails, the result says `committed: false`.

## Reviewing mined correction candidates

Palinode can mine your harness transcripts for moments you told an agent it was
wrong (off by default; see
[HARNESSES.md](HARNESSES.md#mining-transcripts-for-corrections)). Those are
**proposals**, and every one of them — including the ones the classifier was
not confident about, which are marked `needs_review` — goes through the same
review as anything else.

```bash
palinode corrections                      # the queue, with each candidate's id
palinode corrections preview --candidate <id> --target decisions/<slug>
palinode corrections apply   --candidate <id> --target decisions/<slug> \
  --expect-revision <revision> --confirm
palinode corrections dismiss --candidate <id> --reason 'about a code change, not a memory'
```

A candidate carries the user's own bounded span, the session and turn it came
from, and a target only when those words supplied one. When they did not, you
choose the target — nothing is inferred from a span that named nothing.

Applied and dismissed candidates are **marked and kept**, never deleted. That
is what stops the next scan proposing the same sentence again: the queue
de-duplicates on session id plus span hash, read from every row.

If the transcript itself is gone, the preview says the source is unavailable —
and says why that does not weaken the evidence. The span was captured verbatim
from the user's own turn with no model involved; what you lose is the ability
to re-read the surrounding conversation, not the quote.

## Recovery

Undo previews first, like everything else:

```bash
palinode corrections undo --target decisions/harbor-notes-storage
palinode corrections undo --target decisions/harbor-notes-storage \
  --expect-revision <the revision from the preview> --confirm
```

The output names three different things, because only the first is on offer:

- **Restoring a previous assertion** — what this does. The archived record goes
  back to `status: active` and re-enters default recall.
- **Deleting history** — *not offered here.* The commits, the `-history.md`
  sibling and the replacement record all remain. Erasure is
  [a separate, destructive procedure](DATA-LIFECYCLE.md).
- **Undoing external actions** — *impossible.* Anything an agent did while
  acting on the corrected memory — code it wrote, messages it sent, requests it
  made — is outside this store and cannot be reached from here. Restoring a
  memory changes what future sessions read. Nothing else.

An undo will not resurrect something a *different* act retired. A record
carrying a mention-level retraction, or one withdrawn by a forget request, is
refused with a pointer to `palinode unretract` or `palinode forget-withdraw` —
those are deliberate acts, and an undo must not quietly reverse one.

The replacement record is left exactly as it is. Retiring it is a second
decision, with its own preview.

## From the inspector

The local inspector ([UI.md](UI.md)) is **read-only**, and stays that way here.
A memory page carries a *Correct or retire this* section showing the record's
current revision, what else references it, any pending candidates that quote
text in it, and the exact commands to run — nothing on the page writes.

For a record the inspector's listing hides (`private`, `restricted`, out of
scope), the section is replaced by a refusal. The page is loopback-only, which
is not an authorization boundary — it never established who is asking — so it
offers no way to change such a record. Use the CLI or the API, where the
caller's authority is explicit. The page still *renders* the record, labelled
as hidden, because inspecting the history of exactly those records is worth
keeping.

## Every surface, same contract

| | CLI | REST | MCP |
|---|---|---|---|
| Preview | `palinode corrections preview` | `POST /corrections/preview` | `palinode_correction_preview` |
| Apply | `palinode corrections apply` | `POST /corrections/apply` | `palinode_correction_apply` |
| Dismiss a candidate | `palinode corrections dismiss` | `POST /corrections/dismiss` | `palinode_correction_dismiss` |
| Undo | `palinode corrections undo` | `POST /corrections/undo` | `palinode_correction_undo` |

The plugin exposes the same four. Parameter names are identical across all of
them; see [PARITY.md](PARITY.md).

The CLI is TTY-aware: human-readable when you are looking at it, JSON when it
is piped.

## See also

- [QUICKSTART.md](QUICKSTART.md) — the guided first correction.
- [DATA-LIFECYCLE.md](DATA-LIFECYCLE.md) — supersession versus erasure, and
  what a complete erasure actually requires.
- [GIT-MEMORY.md](GIT-MEMORY.md) — `history`, `diff`, `blame` and `rollback`.
- [CLI.md](CLI.md#palinode-corrections) — every option.
