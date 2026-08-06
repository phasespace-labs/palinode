# ADR-018: Epistemic Markers in Memory Files

**Status:** Accepted
**Date:** 2026-06-28
**Amended:** 2026-07-18 — `unverified` added as a 4th settable value (#589; originally deferred, see §4)
**Relates to:** ADR-010 (cross-surface parity contract), ADR-015 (write-semantics axis — the model this follows)
**Tracks:** #72 (this ADR is its spec); foundation for #533 (typed contradiction + evidence-backing links) and #366 (advisory project-memory review)
**Milestone:** v0.9.0 (Memory quality + HTTP transport)

---

## 1. Problem

A Palinode memory declares *what topic* it is about (`entities`), *what kind of
record* it is (`type`: Decision / Insight / …), and *its lifecycle* (`status`,
`update_policy`). It does **not** declare *how much epistemic weight the claim
carries*. A directly-observed fact, a model's extrapolation, and an explicitly
unresolved question are stored identically and recalled identically.

For an audit-grade memory system this is a gap. Three things suffer:

1. **Recall trust.** A reader (human or agent) cannot tell at a glance whether a
   recalled statement is something that was verified or something the system
   inferred. Inferences silently acquire the authority of facts.
2. **Open questions decay invisibly.** "We still don't know whether X" is worth
   recording, but today it is recorded as if it were a settled fact and is never
   resurfaced for resolution.
3. **No anchor for provenance-quality work.** #533's typed contradiction /
   evidence-backing links and #366's advisory review both want to reason over
   *kinds* of claims (an `inference` should carry a `backed_by`; a stale
   `open_question` wants attention). They need a marker to compose over.

## 2. Decision

**Add an optional per-memory frontmatter field `epistemic`** declaring the kind
of claim the memory makes, orthogonal to `type` and `status`:

```yaml
epistemic: fact | inference | open_question | unverified
```

The four **settable** values (what the save surfaces accept and validate):

- **`fact`** — directly observed / verified (an explicit, *earned* claim).
- **`inference`** — derived or extrapolated from other facts (lower trust;
  should ideally carry a `backed_by`, shipped in #533).
- **`open_question`** — unresolved; an explicit marker that this is *not yet*
  settled.
- **`unverified`** — asserted but not checked (#589, added 2026-07-18): a
  positive claim was made, but it was neither verified into `fact` nor derived
  as an `inference`, and it is not a question. Distinct from `unmarked` — a
  claim *was* made, honestly labelled as untested. The prime candidate to
  acquire a `backed_by` link (#533, shipped) and graduate toward `fact`.

**Absence is its own state — `unmarked` — and is NOT equated with `fact`.** A
memory with no `epistemic` field made no epistemic claim. For an audit-grade
store this distinction is the whole point: "nobody declared this" must not
silently inherit the authority of "verified" — defaulting the unmarked majority
to the *highest*-trust category would be exactly backwards. `unmarked` is
**trust-neutral**: neither flagged as a problem (no lint noise) nor asserted as
verified (downstream consumers — #533 backing, #366 review — treat it as "no
claim", not as fact). It is **not a settable value** (reached only by omitting
the field, so it is intentionally absent from `VALID_EPISTEMICS`; the surface
rejects an explicit `unmarked`). Every memory written before this field existed
is `unmarked` and byte-for-byte unaffected. To assert fact-hood a writer sets
`epistemic: fact` explicitly. The field is **persisted only when a marker is in
effect**, so unmarked memories keep clean frontmatter.

(This reverses the initial draft's `DEFAULT_EPISTEMIC = "fact"`. The seam that
forced the change: treating unmarked as fact conflates two different questions —
*was a claim made?* (field presence) and *what kind?* (the value) — and grants
false authority to the un-vetted majority. The provenance panel already rendered
"fact (default)" distinctly from an explicit fact; making the default `unmarked`
makes that honesty load-bearing instead of cosmetic.)

**Sticky.** Like `update_policy` and `created_at` (ADR-015 §2.1/§2.4), the marker
carries forward across a re-save of the same `(category, slug)` that omits it —
re-saving is "the same logical memory," so the claim's epistemic state should
survive a save that forgets to restate it. The integrity argument is decisive:
non-sticky would let a re-save **silently drop** a deliberate `fact`,
`inference`, or `open_question` back to `unmarked` — erasing a claim the writer
deliberately made. To *change* a marker the
writer states the new value explicitly (e.g. `epistemic=fact` resolves an open
question into a verified fact); only silent omission inherits. A memory that was
never marked still writes no frontmatter and reads as `unmarked`, so the
stickiness only ever preserves an intent the writer already expressed.
Resolution order: explicit param > `metadata` value > inherited prior marker >
unset (`unmarked`, unwritten).

Note that stickiness is a **write-path implementation choice, not a property of
the marker vocabulary**. It follows from how this system handles re-saves of the
same logical memory (ADR-015); a different store could adopt the same four values
with different write semantics and lose nothing. The vocabulary and the
absent-is-not-fact rule are the portable parts.

**On naming the absent state.** `unmarked` is this implementation's name for
"no epistemic field was set." It is not a fifth value: the save surfaces reject
it, and it is deliberately absent from `VALID_EPISTEMICS`. Read-side projections
(trace output, the provenance panel) may render the word so that absence is
*visible* rather than blank — but what they are naming is the absence itself.
Anything consuming this field should test for the field's presence, not compare
against the string.

### Where it lives

- **Vocabulary source of truth:** `VALID_EPISTEMICS` + `DEFAULT_EPISTEMIC` in
  `palinode/core/parser.py` (alongside `VALID_STATUSES` / `VALID_UPDATE_POLICIES`).
- **Validated at the save surface** (`/save`): an unknown value is rejected with
  HTTP 400 rather than silently coerced — a typo'd `inferrence` must not land in
  frontmatter. The value is resolved from the first-class param *or* the
  free-form `metadata` dict (the explicit param wins) and validated once, so a
  metadata-supplied value can't slip past unvalidated.
- **Settable on save across all four interfaces (ADR-010 parity):** MCP
  (`palinode_save` enum param), REST (`SaveRequest.epistemic`), CLI
  (`palinode save --epistemic`), plugin (`palinode_save` union literal).
- **Surfaced on recall:** asserted markers (`inference`, `open_question`) are
  labelled in MCP search results (`fact` and `unmarked` stay unlabelled to avoid
  noise); the `/ui` provenance panel renders a **Claim type** row — `unmarked`
  shown trust-neutral, `fact` as an asserted claim, `open_question` in warn
  styling.
- **Lint:** `palinode lint` reports `stale_open_questions` — an
  `epistemic: open_question` older than the stale threshold (90 days) wants
  resolution. Independent of `status` (epistemic state, not lifecycle).

## 3. Consequences

- **Additive and safe.** Optional field, default `unmarked`, written only when a
  marker is set; no migration and no schema-version bump required — `epistemic`
  is simply another optional field under the current implicit schema.
- **Foundation, not the whole story.** `epistemic` says *what kind* of claim a
  memory is; it does not say *what supports it* or *what it conflicts with* —
  that is #533's `backed_by` / `contradicts`, since shipped. An `inference` was
  honestly marked as lower-trust even before #533 gave it a backing link.
- **Composable.** #366's advisory review consumes `open_question` markers (and
  #533's `contradicts` links) as quality signals over the whole project's memory.
- **No portability claim is made here.** These four values are a vocabulary this
  store validates; nothing in this ADR asserts that another system's `inference`
  means the same thing. A memory file carries no declaration that it was written
  under any shared convention, so a reader cannot yet distinguish a deliberately
  marked record from one that merely looks similar. Closing that gap needs a
  published specification to point at, which is deliberately not this document's
  job.
- **Consolidation is unchanged.** The deterministic executor does not infer or
  rewrite `epistemic`; it is a writer assertion. (A future enhancement could let
  consolidation *propose* promoting a resolved `open_question`, but proposing —
  never silently applying — stays the discipline.)

## 4. Alternatives considered

- **A new `type` value (e.g. `OpenQuestion`).** Rejected: `type` selects the
  storage directory and the record's shape; epistemic weight is orthogonal to
  both. An Insight, a Decision, and a ProjectSnapshot can each be a fact, an
  inference, or an open question. Overloading `type` would multiply the type
  vocabulary combinatorially.
- **Reuse `confidence` (0.0–1.0).** Rejected: a continuous confidence score and
  a discrete claim-kind answer different questions. "This is an open question" is
  not "confidence 0.3"; an unresolved question has no meaningful point estimate.
  The two can coexist (an `inference` may also carry a `confidence`).
- **Free-form tag in `metadata`.** Rejected: no validation, no parity guarantee,
  no canonical vocabulary — exactly the drift ADR-010 exists to prevent.
- **Only three settable values (defer `unverified`).** The original decision
  shipped `fact`/`inference`/`open_question` and deferred a 4th
  "asserted-but-not-checked" value to keep the initial change scoped; such
  claims fell through to `unmarked`, conflating "claim made but untested" with
  "no claim made at all". Superseded 2026-07-18 by #589: `unverified` is now
  the 4th settable value (see §2).

  This is worth stating plainly rather than burying in an amendment note: the
  fourth value was **not** designed in. Three values shipped, ran against a real
  corpus, and the gap surfaced as a category of claim that kept landing in the
  wrong bucket. Any closed vocabulary is a bet; this one has at least been
  tested against practice and revised once for a stated reason. That is the
  strongest argument available for holding it closed now — and the reason to
  keep it revisable if a fifth gap surfaces the same way.
