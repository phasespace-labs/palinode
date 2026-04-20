# Compaction Demo — Watching a Memory File Consolidate

This directory walks through a realistic scenario: a team debating REST vs GraphQL for a new checkout API over several weeks, with the decision recorded as it evolves. You see the raw daily-note accretion, then the deterministic executor applying consolidation operations, and finally the clean state.

The key thing to notice: **every change is a git commit, and every line can be blamed back to the session where the fact was first recorded.** That's the "git your agent's brain" pitch, made concrete.

## The scenario

Alice and Bob are building a mobile checkout flow (`projects/my-app.md`). Over three weeks they argue about REST vs GraphQL vs tRPC. Each working session writes notes into daily files and updates to a project status file. After a few weeks the status file is noisy, contradictory, and hard to read — exactly the problem Palinode's compaction solves.

## The files

| File | What it shows |
|------|--------------|
| [`pass-0-initial.md`](pass-0-initial.md) | Raw `projects/my-app-status.md` after three weeks of daily-note appends. 23 status lines. Contradictions. Outdated entries. Duplicate decisions. |
| [`pass-1-after-update-supersede.md`](pass-1-after-update-supersede.md) | After the nightly/debounced reflection pass ran UPDATE and SUPERSEDE ops. 18 status lines. Stale entries marked `[SUPERSEDED]`, corrected entries updated in place, with inline diff comments showing what the executor did. |
| [`pass-2-after-merge-archive.md`](pass-2-after-merge-archive.md) | After the weekly full pass ran MERGE and ARCHIVE. 11 status lines. Related facts merged, superseded entries archived out. Final clean state. |
| [`blame-output.txt`](blame-output.txt) | `palinode blame projects/my-app-status.md` — every remaining line traced back to the commit and session that originated it. |
| [`diff-output.txt`](diff-output.txt) | `palinode diff --days 21` — the journey from pass 0 to pass 2 as a single readable diff. |

## How to read this

Start with `pass-0-initial.md` and notice the mess — every session-end hook append adds another status line, contradictions don't resolve themselves, and old `[ ]` todos stick around long after they're done.

Then open `pass-1-after-update-supersede.md` side-by-side. The executor preserved every original line's provenance but corrected outdated claims (SUPERSEDE) and tightened wording (UPDATE). Note the `<!-- supersede: ... -->` comments — those aren't just documentation, they're structured metadata the executor writes so that the next pass knows what it's looking at.

Then `pass-2-after-merge-archive.md` shows what you'd actually want to read if you opened the file fresh. Related facts have been merged into single lines. Archived entries are gone from this file but preserved in `archive/2026/my-app-status.md` — nothing is deleted, just moved out of the way.

Finally `blame-output.txt` is the demo's money shot: **every line has a commit, and every commit has a session.** You can answer "when did we decide X?" and "which session first mentioned Y?" without remembering — git remembers for you.

## What the executor actually did

Between pass 0 and pass 1 (13 operations):
- 6 × `UPDATE` — tightened wording on stale-but-still-true status lines
- 4 × `SUPERSEDE` — marked contradicted/outdated lines as superseded by specific newer facts
- 2 × `KEEP` — explicitly flagged facts as still current (no-op, but creates audit trail)
- 1 × `NOOP` — the LLM proposed no change for a section that was already clean

Between pass 1 and pass 2 (9 operations):
- 3 × `MERGE` — combined related facts into single lines (e.g. three "tried GraphQL" notes → one)
- 4 × `ARCHIVE` — moved superseded entries to `archive/2026/my-app-status.md`
- 2 × `UPDATE` — final wording pass on merged lines

All 22 operations were **proposed by an LLM** but **applied by deterministic Python** (`palinode/consolidation/executor.py`). The LLM never touches the file directly — it only emits JSON like `{"op": "SUPERSEDE", "id": "f-0317-1", "superseded_by": "f-0324-2", "reason": "stripe integration actually shipped on the 24th"}` and the executor validates and applies it.

## Why this matters

A lot of AI-memory systems will happily let an LLM rewrite a memory file in one shot. That's fast but lossy — you lose the trail from "what we thought on March 15" to "what we actually decided on March 24." Palinode's trick is that the LLM never rewrites; it proposes discrete operations, and every operation is a git commit. You get the speed of LLM-driven consolidation *and* the auditability of an append-only log.

If you want to see this on your own data: run `palinode consolidate --dry-run` and inspect the proposed ops before applying. Compaction is powerful, and you should be able to see exactly what it wants to do to your memory before it does it.
