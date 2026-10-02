# Recall without an embedding service

Set the **server and watcher** to lexical mode before starting them:

```yaml
# PALINODE_DIR/palinode.config.yaml
search:
  retrieval_mode: lexical
```

Or set `PALINODE_RETRIEVAL_MODE=lexical` in their environment. Environment wins
over YAML; invalid values fail startup. Restart running processes after a
change. Clients use the API's mode, so setting this only in an MCP client or
CLI process does not change an already-running API.

Lexical mode indexes and searches current text with SQLite FTS5. It needs no
embedding endpoint, model download, cloud key, or chat service. The existing
Python package and its SQLite/FTS5/sqlite-vec dependencies are still required.
Saving indexes synchronously; the watcher indexes subsequent filesystem edits.
Automatic description/summary scheduling and write-time model checks are
skipped in this mode. Explicit optional model tools (consolidation, semantic
neighbors, trigger matching, and similar operations) still require their
configured services; they are not part of the first-use path.

With an initialized git-backed memory directory and the API running:

```sh
palinode save 'Use SQLite for the Orionledger decision.' --type Decision --slug orionledger
palinode read decisions/orionledger.md
palinode search Orionledger --score
palinode search Orionledger --format json --diagnostics
```

The save receipt reports `retrieval_mode: lexical`, `indexed: true`,
`indexed_fts: true`, and `embedded: false`. Embeddings are intentionally skipped,
not deferred retries. If indexing actually fails, the file remains saved and
versioned and the receipt reports `indexed: false` with `index_error`.

## Search and diagnostics across surfaces

| Surface | Contract |
| --- | --- |
| REST `POST /search` | Existing results array by default. Hits add `retrieval_mode`; lexical hits have `raw_score: null`. Send `receipt: true` for the envelope, including diagnostics on empty results. |
| MCP `palinode_search` | Uses the same REST path; displays mode, readiness, outcome, and the match-confidence verdict from the receipt, leading with `No confident match.` when that is the verdict. Save/read use the shipped tools. |
| CLI `palinode search` | Text shows retrieval diagnostics. JSON stays an array by default; `--diagnostics` returns `{results, receipt}`. |
| Inspector `/ui/memory?q=Orionledger` | Same search handler and qualifiers; shows mode/readiness, labeled keyword rank, and a distinct backend-failure banner. |
| OpenClaw `palinode_search` | Same API mode; displays receipt diagnostics, including empty outcomes and the match-confidence verdict. Plugin CLI JSON search preserves the REST array. Automatic recall uses the same search endpoint; embedding-dependent trigger/associative channels remain optional. |
| Pi/Cline shared recall | Their `/search` channel inherits the server mode and already renders `raw_score: null` as keyword rank. Their resolve channel keeps its explicit keyword-only coverage qualifier. |

`receipt.retrieval` contains `configured_mode`, `active_mode`, `index_state`,
`outcome`, `coverage: visible_indexed_corpus_only`, and — whenever the mode
ranked something against a query — `confidence` plus the `arms` block behind
it (see "Match confidence" below). There are no store-wide
counts in search receipts. Readiness uses the same live visibility gate as
recall; a corpus containing only hidden files is indistinguishable from an
empty visible corpus. It checks indexed file visibility, not every source file
for outstanding edits, so `ready` does **not** promise the watcher has caught up.
Readiness stops at the first visible indexed file; delivered hits already
prove readiness. Hybrid then checks only vectorless files through the live
visibility gate, with no per-file vector query. The existing `/status` administration surface also
reports store-wide chunk/vector counts and skips embedding probes in lexical
mode (`embed_functional` and `ollama_reachable` are `null`, meaning unprobed).

| State | Meaning |
| --- | --- |
| `matched` | Visible indexed text matched; freshness/currency/evidence still qualify its use. |
| `no_match` | No result from the visible indexed corpus. It does not prove no answer exists in unindexed files. |
| `not_indexed` | No indexed file is visible to this caller, including an empty store. Save or reindex visible source files. |
| `embeddings_pending` (index state) | Hybrid is configured but some visible indexed files lack vectors. Run full reindex and check again. |
| HTTP 503 / search failure | Unexpected embedding backend outage in hybrid mode; no empty-success substitution. |
| HTTP 500 / search failure | An index or other unexpected search failure; no empty-success substitution. |

A lexical score is a rank, **not semantic similarity or confidence**. The vector
`threshold` and `hybrid=false` request switch do not enable embeddings or apply
a cosine floor in lexical mode. FTS tokenization, keyword expansion, the shared
ranker, date/type/priority filters, deduplication, live visibility, current-text
projection, freshness/currency, and optional `resolve` evidence remain in use.

## Match confidence

`outcome` says whether rows came back. `confidence` says whether any of them is
worth treating as an answer — a separate question, and one the delivered score
cannot answer, because that score is a fused rank.

The verdict is read from each arm's own **pre-fusion** score, both of which
every delivered row now carries: `raw_score` (real cosine, `null` when the
vector arm did not retrieve the row) and `keyword_score` (normalized BM25,
`null` likewise).

A `keyword_score` is **coverage of the query**, not a fraction of some fixed
maximum: this chunk's BM25 divided by what the query could score against a
chunk holding every one of its terms once. `1.0` is that chunk, and a denser
one — a repeated term, a short chunk — reads a little above it. The scale is
the same on a three-record store and a three-million-record one, which is why
an exact identifier reads as the exact match it is rather than as a weak hit
on a small store.

| Verdict | Meaning |
| --- | --- |
| `confident` | An arm's best delivered score reaches a mark where measured wrong answers do not appear (cosine 0.60, keyword 0.60) — and, in hybrid mode, the other arm at least recognises the query. |
| `weak` | Something matched; nothing reaches that mark. Ordinary for a broad question. |
| `none` | Nothing reaches even the low mark (cosine 0.50, keyword 0.22). The store most likely does not hold this. |

The two arms reaching their confident mark at the same number is a coincidence
of two calibrations; a cosine and a coverage fraction are not comparable
quantities.

`receipt.retrieval.arms` carries each arm's `best`, its own `verdict`, and the
two marks it was judged against, so the verdict is auditable rather than
asserted. `best: null` means that arm contributed no evidence to this delivery
— which is not a score of zero. Every row the retrieval log writes for the
delivery carries the verdict too, including the single row an empty delivery
writes.

**Corroboration.** In hybrid mode — the only mode where both arms run — the
vector arm may not claim `confident` over a query the keyword arm does not
reach its own weak mark on. Such a delivery reports `weak` and
`retrieval.corroboration: "missing"`, and the diagnostics line says so, so the
verdict can never read as contradicting the arm evidence beside it. This is
measured, not defensive: against a whole corpus the vector arm scored three
no-answer questions at cosine 0.607–0.635 — above four answerable questions —
and all three were queries the keyword arm barely registered. No cosine mark
separates those groups; the other arm does. The rule is one-directional (the
keyword arm needs no corroboration, and `confident` would otherwise be
unreachable in lexical mode) and applies only where both arms ran, so a
`hybrid: false` request is unaffected.

This used to carry a cost on small stores — the keyword arm's scale moved with
corpus size, so a store of a handful of records could not produce a
`confident` hybrid delivery however well it matched. Pricing the arm per query
removed it: an exact identifier hit reaches the confident mark on a
three-record store, so it can corroborate there too.

**Results are not withheld for a `none` verdict.** The MCP rendering leads with
`No confident match.` and the weak results follow underneath, because an empty
slate cannot be told apart from an empty store by the reader. Operators who
want the stricter behaviour can set:

```yaml
search:
  abstain_on_no_confident_match: true   # default false
```

which empties the slate **on the MCP surface only** when the verdict is `none`;
the rendering then says how many results were withheld. REST, CLI, the
inspector and the plugin keep returning their rows either way, and the receipt
and retrieval log always record what the store delivered, so the switch changes
what is shown and never what is measured.

Measured effect on the packaged relevance fixture
(`bench/results/relevance-bm25-query-scale-2026-09-22`, all six arms against a
real embedder), with recall@k and top-1 unchanged in every mode:

| Mode | correct abstention | irrelevant injections | withheld slates |
| --- | --- | --- | --- |
| hybrid @ 0.40 (what MCP sends) | 0/14 → **2/14** | 186/262 → 179/255 | 2, both no-answer |
| hybrid @ 0.50 (REST default) | 2/14 → 2/14 | unchanged | none — those slates were already empty |
| lexical (keyword-only) | 2/14 → **8/14** | 128/200 → 103/175 | 6, every one of them a no-answer question |

The stricter the retrieval floor, the less the switch has left to do: on the
fixture it withheld nothing correct in any mode, and nothing at all at the
REST floor.

The keyword marks are absolute on a scale that is now the same on any store
size, and they were re-calibrated on the fixture when it changed (0.22 → 0.60
confident, 0.13 → 0.22 weak). What a coverage score still cannot tell you is
that a query had nothing distinguishing in it: ask a single common word and
every chunk holding it covers the query completely. Both marks, their
measurements and that limit are documented in `palinode/core/confidence.py`.

The verdict never decides what is delivered. Which results come back is the
subject of the next section, and the two are independent.

## A short answer is an answer

`limit` is a ceiling, not a quota: a lexical search returns fewer results than
you asked for when the store has less to say. Two settings decide that, both
under `search:` and both relative to the best candidate in the same result set,
so the top match always survives and a query that has any keyword match never
comes back empty.

- `lexical_fts_threshold` (default `0.35`) — a keyword candidate is delivered
  only when it scores at least this fraction of the best keyword match in the
  same result set. This is the floor for searches where the keyword arm is the
  only arm; hybrid searches use `fts_threshold` (default `0.4`), which is
  stricter because the vector arm is there to re-admit a candidate. Set it to
  `0.0` to fill `limit` unconditionally, as releases before 0.22 did.
- `max_chunks_per_file` (default `1`) — how many chunks of one file may take
  slots ahead of other files' competitive chunks. Overflow is deferred, not
  discarded: it still fills a slate that would otherwise be short, so a capped
  chunk can appear behind results it outscores. Set it to `0` for the old
  unlimited behaviour.

Both were measured on the relevance rig against real SQLite and FTS5; see
`bench/relevance`. The delivery receipt and the retrieval log describe what was
actually delivered, so a shorter slate means fewer rows in both.

Hybrid mode bounds its vector arm the same way, with
`search.vector_relative_floor` — see
[How Memory Works](HOW-MEMORY-WORKS.md#phase-2-topic-specific-search-per-message).
It has no effect in lexical mode, where there is no vector arm to bound.

## Unexpected outages are a separate contract

The default is `search.retrieval_mode: hybrid`. A connectivity, timeout, or
service failure during ordinary search stays `EmbeddingUnavailable` / HTTP
503, including in MCP, CLI, and plugin error presentation. Search does not
silently switch modes. Fix the service or explicitly configure lexical mode.
A deterministic rejection of one input by a healthy embedder retains the
existing `keyword-fallback` behavior, now using the shared qualifier pipeline.
Resolve retains its pre-existing keyword-only degraded-coverage behavior and
also skips embedding entirely in explicit lexical mode.

The indexer's existing hybrid outage behavior is unchanged: cold startup may
write FTS-only rows after a bounded failed probe; a subsequent unexpected
outage during a warm reconcile rolls the transaction back and leaves an
observable retry condition. These are distinct from deliberate lexical indexing.

## Enable embeddings later

1. Configure `embeddings.primary` for the desired endpoint/model/dimensions.
2. Set `search.retrieval_mode: hybrid` (and remove any lexical environment
   override), then restart the API and watcher.
3. Run **`palinode reindex` without `--since`**. Unchanged lexical rows have no
   vector and are therefore re-embedded; no source rewrite is needed to trigger it.
4. Check `/status` retrieval readiness and run a search. Pending vectors mean
   reindex is not complete; a nominal reindex response alone is insufficient.

The fixture verifies byte-identical memory files and unchanged git HEAD while
adding vectors with the same 1024 dimensions. Optional mechanical cross-reference
or summary enrichment can make their usual provenance commits during a normal
reindex. Existing history is preserved. Use the same configured dimensions for this reindex path. Changing model
dimensionality requires a separate rebuild of the derived vector index; this
mode switch does not resize an existing vector table.

## Contract fixture behavior

`tests/test_lexical_retrieval.py` pins the public lexical-mode contract against
real SQLite/FTS storage without requiring an embedding or chat service. The
broader release validation also exercises REST, CLI, MCP stdio, inspector, and
plugin callers. Synthetic embedding fixtures use hand-authored mappings; they
are contract tests, not a real-model semantic-quality study.

For “Use SQLite for the Orionledger decision.”, lexical finds “Orionledger”,
misses the no-overlap paraphrase “Which embedded relational engine did we
choose?”, and returns no hits for “volcanic telescope”. The synthetic hybrid
fixture finds the first two and misses the third. Keyword recall can also
return irrelevant hits that share words; a result is not proof that a question
has an answer. Real-model quality, human first-use timing, and flagship adoption
studies remain separate acceptance work.
