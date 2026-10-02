# Delivery Receipts

A **delivery receipt** is the qualified record of one hand-off of context: which
records were supplied, at which exact source revision, how each was disposed, what
lineage is known behind them, under which policy and caller scope, evaluated on
which clock, and what the next known temporal boundary is.

It records **supplied context, not causal influence**. A receipt never claims that a
delivered record shaped what the agent did next; that edge is a separate, unbuilt
piece of provenance.

Implementation: [`palinode/core/receipt.py`](../palinode/core/receipt.py). A narrative
introduction is in [`HOW-MEMORY-WORKS.md` §10](HOW-MEMORY-WORKS.md#10-delivery-receipts-what-you-were-just-handed).

---

## Fields

### Delivery level

| Field | Type | Meaning |
| --- | --- | --- |
| `bundle_id` | string (16 hex) | Identity of this delivery: a digest of the normalized request, the supplied `(ref, revision)` pairs, and the evaluation time. The correlation key shared by the response, the retrieval log, and any later bundle. A second identical request is a second delivery and gets a different id. |
| `policy_version` | string | `palinode/<package>+projection/<n>+config/<fingerprint>` — see [Policy version](#policy-version). |
| `scope` | array of string | The caller scope chain **as the server resolved it**. `[]` = no scope identity (access control only). Never the caller's own claim. |
| `requested_time` | ISO-8601 | The clock the lifecycle policy evaluated against. |
| `evaluated_at` | ISO-8601 | When this result was evaluated. |
| `next_transition` | ISO-8601 or `null` | The nearest known transition **ahead of** `evaluated_at` among the delivered records: the earliest `expires_at`, or a declared future `date`. `null` means no boundary is known — it never means "never". A malformed or absent date contributes nothing. |
| `supplied` | array | One entry per delivered record (below). |
| `lineage` | array | Origin groups (below). |
| `coverage` | object | `{"status": "complete" \| "partial" \| "not_requested", "reasons": [...]}`. `partial` reasons come from the closed evidence vocabulary. `not_requested` means no evidence was gathered at all — distinct from `complete`, because nothing was looked for. |
| `dispositions` | object | Counts per disposition, over `supplied`. |

### `supplied[]` — one delivered record

| Field | Meaning |
| --- | --- |
| `ref` | The memory ref (path without `.md`). |
| `revision` | The exact source revision, or `null` when the delivery never computed one. |
| `revision_basis` | Which hash domain `revision` is in: `index_section_sha256` (the raw per-section hash `store.check_freshness` compares against — what a search hit carries), `file_sha256` (the whole file as read — what a `/context/prime` row and an evidence record carry, and what a `resolve` record carries whenever `freshness` is `stale`, because under index lag the delivered text came from the file and the indexed hash describes nothing that was supplied), or `unknown` (the delivery never computed one). **The domains are never compared across surfaces**, which is why the basis is named on every record. |
| `freshness` | Index/source agreement: `valid` / `stale` / `unknown`. `null` where the surface computes no index comparison. |
| `currency` | Whether the assertion is in force: `current` / `unmarked` / `retired` / `contested`. |
| `span_integrity` | Whether the record's cited quote anchors still match their sources. |
| `disposition` | See below. |
| `origin` / `origin_kind` | The support anchor this record rests on (`claim` / `source` / `backed_by`), or `null` / `unknown` — which means the record named no anchor, **not** that it was shown to be independent. |

### Dispositions

A closed vocabulary:

| Value | Meaning |
| --- | --- |
| `selected` | The record that stands as the answer (or, without resolution, a hit with nothing against it). |
| `replaced` | Retired — superseded, archived, retracted, or expired. |
| `conflict_side` | A visible side of a conflict nothing mechanical settles. |
| `insufficient` | The evidence around the hit does not settle it. |
| `evidence_only` | Supplied as evidence around a hit, not as an answer. |

### `lineage[]` — one origin group

| Field | Meaning |
| --- | --- |
| `origin` / `origin_kind` | The anchor the group's members cite, and how it was established. `null` / `unknown` for a record that anchors nothing. |
| `origin_revision` | The origin's revision **as this delivery supplied it** — set only when the origin is itself one of the supplied records. `null` otherwise: the origin was named, not delivered. |
| `members` | The records resting on that origin. A snapshot and a session summary citing one observation are **one group**, not two witnesses. |
| `status` | `known` or `unknown`. An `unknown` group is one record's un-established lineage, never a claim of independence. |

### Policy version

Three parts, deliberately the smallest honest set:

- **package** — the installed `palinode` version. Selection, lifecycle, visibility and
  resolution policy are code.
- **projection** — `PROJECTION_VERSION`, the version the current-text projection behind
  every delivered excerpt was produced under. It moves independently.
- **config** — an 8-hex fingerprint over the configured values that decide what is in
  scope and how far evidence may look (`scope.enabled`, `scope.prime_mode`,
  `search.exclude_status`, the `search.evidence.*` budgets). Configuration is edited
  without a release; without this the tuple would claim an unchanged policy across a
  real policy change.

Prompt versions are deliberately **not** in it: no prompt runs on the delivery path.

---

## Two views

| View | Carries | Returned by |
| --- | --- | --- |
| **public** | Every field above: refs, revisions, dispositions, lineage, coverage, scope, times. **No memory text, no titles, no excerpts, and not the caller's query.** | Every delivery surface. |
| **diagnostics** | The public view plus the request fingerprint (which includes the query prose), the surface, the resolve mode, the evaluation window and the reuse key. | Nothing. Internal — for operators reading their own store. |

A third, minimal shape — the **reference** (`bundle_id` + `evaluated_at`) — is what a
delivery that gathered no evidence returns.

---

## Surfaces

### MCP — `palinode_search`

The rendered result gains a receipt block. Without `resolve` that is **one line**
(`Receipt: <bundle_id> · evaluated <time>`); with `resolve` it is the full public view
— a line per supplied record with its revision and disposition, the lineage groups
where copies share an origin, and the coverage. No new tool parameter: an agent is
never asked whether it wants provenance for what it was just handed.

### REST — `POST /search`

`/search` returns a bare JSON array and that stays true. Set `receipt: true` on the
request and the response becomes:

```json
{
  "results": [ ... ],
  "receipt": { ... }
}
```

`results` is byte-identical to what the same request returns without the flag. The
receipt is the **public view** when `resolve` is on, and the two-field **reference**
when it is not. Omit the flag and the response is today's array, unchanged.

Both shapes add a `retrieval` block: the mode, index readiness and `outcome` the
search ran under, and — whenever the mode ranked something against a query — the
delivery's `confidence` verdict (`confident` / `weak` / `none`) with the per-arm
evidence behind it. `outcome` says whether rows came back; `confidence` says whether
any of them is worth treating as an answer, which the delivered score cannot say
because it is a fused rank. See
[`lexical-retrieval.md`](lexical-retrieval.md#match-confidence).

### REST — `POST /context/prime`

The digest response gains a top-level `receipt` (public view): every supplied ref at
its `file_sha256` revision, its disposition, the known lineage, the resolved scope and
policy, the evaluation time and the next known transition. A contested row is a
`conflict_side`, not a quiet `selected`.

No retrieval-log rows are written for a prime — a session-start injection ledger is a
separate contract, and this endpoint has never written to that log.

### REST / MCP / CLI — `resolve` (the bounded-resolution bundle)

The bundle carries its receipt in two places: `receipt_ref` (the `bundle_id`) and
`receipt` (the public view), and the rendered `text` ends with a
`Receipt: <bundle_id>` line, so the id is available to a reader that only ever sees
the injected string.

`supplied[]` covers every record the bundle **delivered**, which is more than the
records it printed in full: the sides of a conflict the budget omitted are delivered
by ref in the omission notice, and what a standing assertion replaced or rests on is
delivered as a pointer. Each one carries the disposition the bundle itself assigned —
`selected`, `replaced`, `conflict_side`, `insufficient`, `evidence_only` — rather than
a disposition re-derived from currency, which could disagree with the payload it is
the receipt for. `coverage` is the bundle's own coverage, packing reasons included.

Revisions come from the index (`index_section_sha256`) when the record is indexed and
from the whole file the evidence layer read (`file_sha256`) when it is not — the two
domains are named, never compared.

**No retrieval-log rows are written for a resolve.** The operation runs on every
prompt; it records no recall and no retrieval event, and having a receipt does not
change that. A receipt describes a delivery; it is not an event in one.

### CLI

`palinode search` and `palinode prime` print the bundle id in text mode. `--format
json` output is unchanged (the results array / the digest object), so existing scripts
keep parsing what they parsed.

---

## Where receipts are recorded

On the retrieval-event log Palinode already writes — `.audit/retrievals.jsonl` — not a
parallel ledger. Each row it wrote before now additionally carries `bundle_id`,
`policy_version`, `scope`, `revision`, `revision_basis`, `disposition`,
`lineage_group`, `coverage`, `next_transition` and the delivery's `confidence`
verdict. Rows from one delivery join on `bundle_id`.

- **Additive, no migration.** Every field is optional; a line written before receipts
  existed reads back exactly as it did. The log is append-only, so re-running against
  an existing log is idempotent.
- **No prose.** Refs, hashes and dispositions are written; memory content never is.
- **Same regime.** The log keeps the visibility and retention it already had
  (telemetry, excluded from semantic recall).
- **The same hit set.** Search evidence stays in the response. Resolve persists a
  receipt-only envelope including evidence refs, excluded from recall statistics;
  those still count only retrieval events.

`palinode trace <file>` reads it back: `recalled` now reports the deliveries a file was
supplied in (`bundles`), the dispositions it was supplied under, and the distinct
source revisions it was supplied at.

---

## Explaining a delivery after the fact

`trace` answers "which deliveries was this *file* in?". The other direction — "what
was *this delivery*, and why was each memory in it qualified the way it was?" — is
`palinode explain <bundle_id>`, `GET /explain/{bundle_id}`, the `palinode_explain` MCP
and plugin tools, and the local inspector page at `/ui/delivery/{bundle_id}`.

It is composition over the storage above: no new table or file. Everything it shows
is a value the log recorded, a value derived from one,
a comparison between a recorded revision and the file today, or an explicit
`unavailable` marker naming why there is no answer.

### What it shows

| Section | From |
| --- | --- |
| Supplied records — ref, revision, revision basis, disposition, lineage group, rank, score | The rows that join on `bundle_id` |
| Whether each source has changed since delivery | `store.check_freshness`, comparing the **recorded** revision against the file now. Only `index_section_sha256` revisions are comparable; a `file_sha256` revision is reported as a different hash domain rather than compared across domains |
| Scope, policy version, coverage, next transition, evaluation time | The delivery-level receipt fields on the same rows |
| Selection path | Search records `source` and `mode` (`explicit` = asked for, `passive` = offered). Resolve records seed inputs, resolution/packing steps and per-record roles; caller demand remains unavailable |
| Dispositions | Counted over the visible supplied records |

### What it cannot show, and says so

Every absent field is rendered as `{"available": false, "reason": …, "detail": …}` from
a closed vocabulary, never omitted and never guessed:

| Reason | Means |
| --- | --- |
| `instrumentation_disabled` | Retrieval capture is off for this store (`PALINODE_INSTRUMENTATION_DISABLED`, or `instrumentation.capture_retrievals: false`), so nothing was written. Checked live. |
| `log_absent` | `.audit/retrievals.jsonl` does not exist — never written, or removed since. Checked live. |
| `no_matching_rows` | The log holds rows, but none carry this reference. The oldest row still present is reported so a reader can see whether the delivery predates the log's horizon. |
| `surface_writes_no_rows` | The delivery came from a surface that writes no rows at all. **Distinct from `no_matching_rows`**: there is nothing to find, rather than nothing found. |
| `predates_receipts` | Rows written before receipts existed carry no `bundle_id`; the count of such rows is reported. |
| `not_recorded` | This delivery did not record the field. Search rows omit lifecycle clock and evidence-only records; resolve receipts retain both. Neither records the project-resolution source or core/trigger/associative attribution. |
| `revision_basis_not_comparable` | The recorded revision is a whole-file hash; the freshness check compares index-section hashes and the two domains are never mixed. |
| `withheld_diagnostics_only` | The caller's own query prose **and** the delivery's session id. See [Who may read the query](#who-may-read-the-query) — asking for the diagnostics view is not by itself enough. The MCP and plugin tools cannot ask for it at all. |
| `withheld_visibility` | A supplied record the caller may not see. It is counted, never named. |

Because a bundle id carries no surface, "this reference came from a surface that does
not log" cannot be *checked* from the id alone. A lookup that finds nothing therefore
reports every candidate cause with a `checked` flag — two are settled from the running
store, two are evidenced from the log's own contents, and the surface question is
marked as uncheckable rather than guessed.

### Who may read the query

Two fields on a delivery describe **the person who asked**, not the memory that was
supplied: the `query` prose and the `session_id`. The visibility gate protects
records; it does nothing for these.

**A bundle id is not a secret.** It is a short deterministic digest that a delivery
hands back, that gets pasted into issues and chat, and that `GET /trace/{file}` already
lists — `recalled.bundles` names every delivery a file was supplied in, and
`recalled.sessions` names the sessions. So holding an id must not be what authorises
reading someone else's words.

Asking for `view=diagnostics` is therefore a request, not an entitlement. It is granted
on either of two grounds:

| Ground | Meaning |
| --- | --- |
| **Loopback bind** | The API is bound where nothing off this machine can reach it (`PALINODE_API_HOST`, falling back to `services.api.host`). This is the load-bearing one, and it is the *same predicate* the local inspector hard-refuses on — one function, so the page and the JSON route cannot drift. |
| **Session match** | The caller presents `session_id` equal to the one recorded on the delivery — a remote session reading back its own delivery. |

Anything else gets the ordinary **public view**, with both fields marked
`withheld_diagnostics_only` and the reason attached. Deliberately **not a 403**:
refusing with a status code would answer a question about the bundle that the caller
has not earned. The refusal wording is identical whether or not the delivery recorded a
session id, because a message that varied would disclose the very fact being withheld.

The recorded `session_id` rides this gate too, and not for tidiness: disclosing it in
the public view would hand a stranger the exact value the session-match branch accepts
as identity.

**Be clear about how strong each ground is.** The bind is a real boundary. The session
match is a *correlation check, not authentication*: a session id is caller-supplied and
unverified, and `GET /trace/{file}` already reports the sessions a file was recalled in,
so a caller who can reach that route can present one. It exists so a legitimate remote
session can explain its own delivery. On a store where query prose is sensitive and the
API is reachable off-box, **bind to loopback** (or put the API behind something that
authenticates) rather than relying on the session branch.

Per surface:

| Surface | Query and session id |
| --- | --- |
| Inspector `/ui/delivery/{id}` | Shown. The page is loopback-guarded, and it evaluates the same predicate rather than assuming it. |
| `palinode explain --diagnostics` | Shown against your own loopback-bound API. Against a remote one it is refused unless `--session-id` names the session that made the delivery — and the CLI prints the withheld marker *and the reason*, so it never quietly shows less than you asked for. |
| `GET /explain/{id}?view=diagnostics` | Per the table above. |
| `palinode_explain` (MCP, plugin) | Never. Both send the public view and have no parameter that could ask for anything else. |

### Which surfaces write rows

| Surface | Writes retrieval-log rows? |
| --- | --- |
| `POST /search` with a query (MCP, CLI, REST, plugin) | **Yes** — one row per delivered record, or one call-level row with `disposition: none_delivered` when the search delivered nothing |
| `GET /read`, `/blame`, `/history`, `/trace` | Yes — a file-read row, with no receipt fields |
| `POST /search` with an empty query (the recency branch) | No |
| `POST /context/prime` | No |
| `POST /resolve` (hook, MCP, CLI, REST, plugin) | Receipt-only envelope; no file-retrieval events |

A receipt from one of the two "No" rows is available **only in the response that
carried it**. Nothing recovers it later, and `explain` says so rather than implying the
delivery never happened.

Resolve stores the **already-built public receipt**, its record qualifiers and roles,
selection steps (query/ref/context → evidence → resolution → packing), and output
budget. The same `explain` API, CLI, MCP and plugin render these fields. No memory
text, titles, excerpts, query prose or session id are persisted for resolve; supplied
wording remains unavailable. Records and ref-bearing qualifiers are visibility-filtered
again on lookup. Receipt entries do not increment retrieval statistics or recall metadata.

The write is one best-effort append after the response text is built, capped at 256 KiB,
with no search, embedding, git write or retry. The recall pause, automatic
project/path exclusions, malformed controls, `instrumentation.capture_retrievals: false`
and `PALINODE_INSTRUMENTATION_DISABLED=1` suppress it. The capture pause does not: the
receipt records a delivery that already happened and carries no memory content, like a
search row, so a delivered receipt id stays explainable while capture is paused. An I/O failure or oversized
receipt leaves delivery unchanged and makes later lookup unavailable. Historical resolve
ids from before persistence was enabled cannot be reconstructed.

### Availability, retention and restart

- **Availability** is the log's, not a database's: a delivery is explainable for exactly
  as long as its rows are in `.audit/retrievals.jsonl`.
- **Retention**: Palinode appends and never rotates or prunes. Anything else that trims
  the file — an external log rotator, a cleanup, a fresh memory dir, a store copied
  without `.audit/` — removes the only record of those deliveries.
- **Restart**: nothing is held in memory, so a restart loses nothing. Turning capture
  off and on again leaves a gap for the window it was off, and `explain` reports
  `instrumentation_disabled` when it is off *now* — which is a statement about now,
  not proof about then.
- **After the fact, never reconstructed**: the only thing read from the current store is
  whether a recorded revision still matches its file. Titles, bodies, current freshness
  and current scope are deliberately not read. A delivery is explained by what was
  recorded at delivery time, or not at all.

### Questions this storage cannot answer

Recorded here as evidence for a later decision about a fuller ledger, not as a plan to
build one:

- *Which sessions ever received this memory via `/context/prime`?* Prime writes no rows.
- *Which sessions received it through a per-turn `resolve` injection?* Receipt lookup
  explains a known delivery id; it does not record session identity or provide reverse session search.
- *What did a search delivery supply as evidence around its hits?* Only the search
  hit set is logged. Resolve receipt envelopes include every supplied evidence ref.
- *What was the lifecycle clock a search delivery evaluated against?* Search logs
  only evaluation time; resolve envelopes also retain `requested_time`.
- *Why was this delivery scoped to this project?* The resolved scope chain is logged;
  the resolution source (`project_resolved_by`) is in the response envelope only.
- *Did an agent read, use, or act on any of this?* Not recorded anywhere — the
  consuming-action edge (`G3`) is not built, and `explain` states that rather than
  implying use from delivery.

---

## The cross-request reuse contract

Palinode caches **per request** today. Nothing caches across requests, and this
document does not introduce one. What it does introduce is the eligibility contract a
future cross-request cache must satisfy — recorded next to the data that satisfies it,
and pinned as `receipt.reuse_key(...)` so the eventual cache consumes a derivation that
was reviewed here.

A previously delivered bundle may be reused only if **all five** still hold at the
moment of the new delivery:

1. **Server-resolved caller access.** The scope chain the *server* resolves for the new
   caller equals the one on the receipt.
2. **Query scope.** The normalized request (query, filters, limits, tiering, resolve
   mode) is identical. Telemetry-only fields such as `session_id` are excluded — they
   change nothing about what is selected.
3. **Policy version.** Unchanged, including the config fingerprint. Widening an
   evidence budget is a policy change.
4. **Source revisions.** Every `(ref, revision)` pair still matches the store. One
   changed file invalidates the bundle. An `unknown` revision can never be shown to
   still match, so a bundle carrying one is not reusable.
5. **Time-sensitive applicability.** The delivery still falls inside the same temporal
   window — the interval bounded by the nearest known transition behind and ahead of
   the evaluation clock.

Point 5 is the one that is easy to get wrong: **an unchanged file is not evidence of an
unchanged answer.** A record with `expires_at` at noon is current at 11:59 and expired
at 12:01 with nothing written in between, so the key differs across that boundary even
though every revision is identical.

Equal keys are a **necessary** condition, not a sufficient one. A cache must still
re-check caller access and applicable time at delivery, because both can change without
any request or file changing. Claim-validity transitions beyond `expires_at` and a
declared future `date` are not modelled here; richer temporal semantics are separate
work.

### The one cache that applies it today

Support checks (`palinode.core.revalidation.SupportCache`) are computed **per
request** and reused inside it only under all five conditions above, applied to one
record's check rather than to a bundle:

| Condition | How the support cache tests it |
| --- | --- |
| Caller access | The scope chain the server resolved must equal the one the entry was stored under. |
| Query scope | The record's own ref and declared backing policy — a support check is not a query. |
| Policy version | The same `PolicyVersion` string a receipt records. |
| Source revisions | Every `(ref, revision)` the walk read is re-read through the caller's reader and must still match. An `unknown` revision can never be shown to match, so an entry carrying one is never reused. |
| Applicable time | The transition pair bracketing the clock, derived from the `expires_at` / declared `date` values the walk actually saw, must be the same one. |

The fifth is the one that bites without any file changing: a source with `expires_at`
at noon supports its dependent at 11:59 and has withdrawn that support at 12:01, so a
result warmed before the boundary is not served after it. The expired grant is not
deleted — it stays readable as history; what changed is whether it may be presented as
currently supporting anything.

### Revalidation receipts are not delivery receipts

Both name a revision, and they answer different questions. A *delivery* receipt records
what was supplied and at which revision; it is written by the server, per delivery,
into the retrieval log. A *revalidation* receipt (`revalidated:` frontmatter) is
authored on a record and records that its author checked a named source at a named
revision — the explicit form of the convention that re-saving a dependent meant it had
been re-verified. They share the `file_sha256` revision basis, and nothing else: see
[EXECUTOR-SPEC](EXECUTOR-SPEC.md#revalidated).
