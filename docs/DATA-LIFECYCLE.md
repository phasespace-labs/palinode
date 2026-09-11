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

## Two operations that must not be confused

**Supersession is not erasure.** Palinode's default for outdated or contradicted
memories is to mark them — superseded, contradicted, archived — while keeping them
retrievable. That is a *feature*: an auditor can ask what was believed and when. It is
also, deliberately, the opposite of erasure.

**Erasure is a distinct, destructive operation.** When the obligation is "this person's
data must be gone," marking is not enough. Palinode supports erasure; it is destructive
by design, and the *act* of erasure is itself recorded (a tombstone that names what was
removed and when — without the content).

## Where a memory actually lives

A complete erasure has to cover every copy. In a Palinode deployment there are five:

| Location | What it holds | Removed by |
|---|---|---|
| The markdown file | The memory itself | Deleting the file |
| The SQLite index | Embeddings + FTS terms derived from it | Reindex after deletion (the index is derived; it can always be rebuilt from files) |
| **Git history** | Every past version of the file | **History rewrite — deleting the file does *not* touch this** |
| Audit/retrieval logs | What was searched and recalled — which can restate memory content | Log purge |
| Remotes, clones, backups | Everything above, elsewhere | Re-mirroring after the rewrite; expiring backups |

The third row is the one that surprises people, and it is the honest core of this page:
**in a git-backed store, `rm` is not erasure.** The file's entire edit history remains
recoverable from the repository until the history itself is rewritten.

## The erasure procedure

1. **Identify the blast radius.** Search for the subject across memories, entities, and
   aliases. Personal data rarely lives in one file: check entity references, wikilinks,
   and memories that *quote* the affected content (`sources[].quote` spans in other
   files restate source text verbatim).
2. **Delete or redact the files.** Whole-file removal where the memory is about the
   subject; in-place redaction where a file merely mentions them.
3. **Write the tombstone.** A small record of the erasure act — date, scope description,
   request reference — with none of the erased content. This is what keeps the audit
   property honest: the ledger shows *that* something was removed without preserving
   what.
4. **Rewrite history.** Use `git-filter-repo` (or equivalent) to remove the affected
   paths/content from all commits. This changes every subsequent commit hash — which is
   precisely why it is the correct tool: erasure *should* be loud in an audit-grade
   system, not quiet.
5. **Force-update remotes and invalidate clones.** Every mirror must be replaced, and
   any clone you control re-cloned. A clone you do not control is out of your erasure
   power — the same limitation every distributed VCS deployment has.
6. **Rebuild the index.** Delete the SQLite database and let the watcher rebuild from
   the now-clean files. Never edit the index instead of the files; it is derived state.
7. **Purge logs and expire backups.** Audit/retrieval logs can restate memory content
   and must be included in the sweep. Backups either get the rewritten history or an
   expiry date inside your compliance window.

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
