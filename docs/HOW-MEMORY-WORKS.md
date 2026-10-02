# How Palinode Memory Works

A complete guide to how context flows through the system — from capture to recall to consolidation.

---

## The Memory Lifecycle

```mermaid
graph TD
    subgraph "Real-Time (every turn)"
        MSG[User Message] --> CHECK[Trigger Check]
        CHECK -->|Fired| INJECT
        CHECK --> RECALL{Recall Engine}
        RECALL -->|Phase 1| CORE[Core Memory<br>core:true files]
        RECALL -->|Phase 2| SEARCH[Hybrid Search<br>BM25 + Vector]
        RECALL -->|Phase 3| ASSOC[Associative Search<br>Entity Graph]
        CORE --> INJECT[Context Injection<br>&lt;palinode-memory&gt;]
        SEARCH --> INJECT
        ASSOC --> INJECT
        INJECT --> AGENT[Agent Response]
        AGENT --> CAPTURE[Session Capture<br>→ daily/YYYY-MM-DD.md]
    end

    subgraph "Weekly (Sunday 3am)"
        DAILY[daily/*.md] --> CONSOLIDATE{Consolidation<br>OLMo 3.1}
        CONSOLIDATE --> SUMMARIES[Project Summaries<br>projects/*.md]
        CONSOLIDATE --> DECISIONS[Decision Updates<br>decisions/*.md]
        CONSOLIDATE --> INSIGHTS[Cross-Project Insights<br>insights/*.md]
        CONSOLIDATE --> ARCHIVE[Archive Processed<br>archive/YYYY/]
    end

    subgraph "On Demand"
        ES[-es Quick Capture] --> ROUTE{Smart Router}
        ROUTE -->|URL| INGEST[Ingest → research/]
        ROUTE -->|Long text| RESEARCH[ResearchRef]
        ROUTE -->|Short text| INSIGHT[Insight]
        SAVE[palinode_save] --> FILES[Memory Files]
        INGEST_TOOL[palinode_ingest] --> RESEARCH
    end

    CAPTURE --> DAILY
    FILES --> WATCHER[File Watcher]
    WATCHER --> INDEX[SQLite-vec + FTS5]
    INDEX --> SEARCH
```text

---

## 1. Session Recall (Every Agent Turn)

**Hook:** `before_agent_start` in the OpenClaw plugin

Every time you send a message, Palinode injects relevant context **before the agent sees your message**. This happens in four phases.

To see the session-start digest yourself — the same one the SessionStart hook warms and the MCP `session_init` tool returns — run `palinode prime` from the project directory (see [CLI.md](CLI.md#palinode-prime)).

### Choosing the project

Startup recall, ambient CLI/MCP search and session-end use one project resolver.
The order is:

1. An explicit `project` argument (a slug or entity ref).
2. The pinned `PALINODE_PROJECT` setting, when `context.enabled` is true.
3. The supplied directory's basename in `context.project_map`.
4. The repository origin name, then the common checkout name, in that map.
5. With `context.auto_detect: true`, infer from the origin name, then the common
   checkout name, then the normalized directory basename.
6. Otherwise none — the digest degrades to core memories and search is unscoped.

**An argument scopes one call; the setting scopes a client.** Nothing persists a
per-call argument: `palinode_session_init(project=…)` scopes that call, and the
search after it resolves independently. Pin the project instead when every call
from one client should agree — `palinode init --pin-project` writes it into the
generated `.mcp.json`, and `palinode mcp-config --stdio --project <slug>` emits
it for a client you configure by hand. Both set `PALINODE_PROJECT` in that
client's own MCP process, so two clients can name different projects without any
shared session state.

A pinned value that is not a slug or a `project/<slug>` ref is **refused**, not
quietly replaced by git inference: the CLI aborts, the MCP tool returns an
error, and the API answers `400`. The error names the setting and never echoes
the value.

Every response that carries scope names the project *and* the source that
decided it — `explicit`, `environment` (the pinned setting), `project_map`,
`git_origin`, `git_common_dir`, `cwd_basename`, `disabled` or `none`. Search
leads with `Scope: project/<slug> (<source>)` on the MCP and CLI surfaces and
returns `project` / `project_resolved_by` in the REST receipt envelope; the
session-start digest carries the same two in its heading and its JSON.
`palinode resolve` and triggers are not project-scoped and report no scope.

Git inspection is local, read-only and bounded by timeouts. It does not fetch.
For example, a checkout renamed to `checkout-2` and its linked worktree
`fix-timeout/src` both resolve to `project/my-app` when their origin ends in
`my-app.git`. Without an origin, linked worktrees use the common checkout name.
`project_map: {my-app: customer-app}` maps both to `project/customer-app`.
A mapping for the task directory itself remains an explicit override.
Repositories with identical names need distinct mappings or explicit projects.

`palinode prime --format json` reports `project`, `project_resolved_by` and
`project_known`. The text digest also shows the basis and known/unrecognized
state. **Known means that at least one visible, current record in the digest's
source scan carries that entity.** It does not certify ownership or correctness.
An inferred name with no such records is labelled unrecognized; hidden and retired
records do not change that label. Its project sections remain empty rather than
substituting another project's records. Global core memories can still appear.

CLI and MCP search use `CWD` from the environment before the process directory;
CLI prime and MCP session-init use an explicit `cwd` before that same default.
Session-end uses the `cwd` supplied by the caller or hook. An API request never
falls back to the API host's process directory. **A remote API host cannot inspect
a checkout that exists only on your computer:** pass `project` explicitly to
prime/session-end and set `PALINODE_PROJECT` for ambient CLI/MCP search. A remote
or unavailable path can otherwise only supply a basename candidate. Bare API
search uses its explicit `context` list; only with **no** `context` field does
it apply the API process's own pinned `PALINODE_PROJECT`, and nothing else — it
never infers the server's directory. An explicit `context` is the caller's
decision and is never overridden, so an **empty list means "no project scope"**
and is how a caller opts out on a pinned server. What the response reports is
always what the search applied. `palinode search --no-context` sends exactly
that empty list; the CLI and MCP always state the scope they resolved, empty
included, rather than leaving the field absent.

`context.enabled: false` disables all ambient project inference, including the
environment and mappings. An explicit project still works. `auto_detect: false`
disables inferred candidates while retaining configured mappings and overrides.
Search context boosts the project's records and also isolates them. A search
with a project leaves out records tagged to a *different* project: their
`entities` name a `project/*` and none of them is this one. Records that name no
project are global and stay. `include_other_projects` (CLI
`--include-other-projects`) returns the rest too, each labelled with its
project. The receipt's `retrieval.other_projects_withheld` counts what was left
out. The same rule applies to the per-turn resolve and the session-start prime.
Project names compare case-insensitively, and a `project/` group in the store's
curated `entity-aliases.yaml` makes each member count as the group's canonical
project; a request resolved to an alias is reported as the canonical project.
Stored tags are never rewritten. `project_map` lookups stay exact. See
[ENTITY-ALIASES.md](ENTITY-ALIASES.md) for the file's format and how to curate
it with `palinode aliases`.

Save remains explicit: use `palinode save ... --project my-app`, the MCP/API
`project` parameter, or `entities: [project/my-app]` for project-wide decisions.
For temporary branch/task observations, state the task and its limits in the
record, use existing `metadata.scope` (for example `session/<id>`) and an
`expires_at` or `ttl` when appropriate, and avoid `core: true` unless it should
be supplied generally. Session scope requires a matching caller scope chain;
entity tags alone are not access control. A worktree name grants no authority.

### Phase 1: Core Memory (always injected)

Core memory files are marked with `core: true` in their YAML frontmatter. These are the facts the agent should always know — who you are, what you're working on, key decisions.

**Smart injection — only when needed:**

| When | What's Injected | Tokens |
| --- | --- | --- |
| **Turn 1** (session start) | Full content of all core files (up to 8K total) | ~2,000 |
| **After compaction** | Full content again (context was just summarized) | ~2,000 |
| **All other turns** | Controlled by `mid_turn_mode` (default: nothing) | 0 |
| **Fallback** (every 200 turns) | Full content (last resort if compaction hook fails) | ~2,000 |

Core memory persists in the model's context window from turn 1 until OpenClaw compacts the session. The compaction hook (`after_compaction`) detects when context was summarized and triggers a full re-injection on the next turn. This eliminates periodic timer-based re-injection, saving ~50K tokens per long session.

**Currently ~6 core files:**

- `people/alice.md` — who you are
- `people/alice.md` — your collaborator
- `projects/my-app.md` — current project status
- `projects/palinode.md` — memory system status
- `projects/infrastructure.md` — infrastructure notes and service status

**Core memory can expire.** A `core: true` memory is *acting* state — it is injected without anyone asking — so it may carry the same `expires_at` (ISO-8601) that ephemeral memories use for the TTL sweep, plus an optional free-text `authority` naming who or what licensed it to act (`"paul: standing"`, a session id, a policy name). Set both through `metadata` at save time (`metadata: {expires_at: ..., authority: ...}`, or `metadata.ttl` for a duration). Past its `expires_at` a core memory stays on disk, in git, and searchable — it just stops being injected: `GET /list?core_only=true` (the session-start hook and the harness plugins) and `/context/prime` (`palinode_session_init`, `palinode prime`) both withhold it, and the lapse is logged once. `authority` is stored and displayed, not enforced. `palinode lint` lists core memories with no `expires_at`.

**Retiring a core memory withholds it the same way.** Expiry is one of several ways a memory stops standing; the injection path asks the lifecycle classifier below about all of them, so a `core: true` decision you supersede in place (`palinode archive <ref> --superseded-by <ref>`) is no longer injected beside the decision that replaced it. Its `/list` row reports `core: false` and names the signal in `core_retired_reason` (`superseded_by: decisions/…`, `status:archived`, `expired`). It is withheld from injection only: the record stays listed, readable and searchable, and the browse surfaces keep showing it with that reason — `palinode list --core` and the `palinode_list` MCP tool ask for it explicitly (`include_retired_core=true` on `GET /list`, off by default so injection is the fail-safe).

**The startup payload has a budget, and truncation never manufactures certainty.** What the digest renders is capped by `context.injection_max_chars` (default 6000) and `context.injection_max_tokens` (default 1500, estimated at 4 chars/token — an estimate, not a tokenizer); the per-turn recall block is budgeted separately by `context.recall_max_chars` / `recall_max_tokens` (3000 / 750), because one payload is paid once a session and the other on every message. Set a pair to `0` and that surface is bounded only by the digest's own line and count limits, exactly as before. Over budget, the digest packs in priority order — snapshots, core memories, decisions, action items — and degrades in one direction only: a plain row may be demoted to its gist and pointer (title plus file path), but **a row never loses a qualifier, and a conflict never loses a side**. A contested row that does not fit is replaced by an explicit stub — `⚠ 2 conflicts omitted for budget — see decisions/a.md, decisions/b.md` — which keeps the source pointers, so a budget can cost you detail but can never make a contested claim look settled. Every omission is reported: `_budget` on the JSON response (cost, count omitted, reason) and a WARNING in the API log. A `core: true` memory should be a gist and a pointer for the same reason; `palinode lint` flags ones over `context.core_gist_max_chars` (1500).

**The digest never presents a retired memory as current.** Every section of the session-start digest — core memories, recent decisions, open action items, recent snapshots — is selected through one lifecycle classifier (`palinode.core.lifecycle`), the same one consolidation uses to decide which decisions govern a compaction. A memory is *retired* when its `status` (or KU `lifecycle`) is `archived`, `deprecated`, `superseded` or `retracted`, when it carries a `superseded_by`, or when its `expires_at` has passed. A memory is also retired **by location**: anything under `archive/` is retired whatever its frontmatter says, because the weekly pass and `archive_memory` move a note there without rewriting it, so its path is the only record that it was retired at all (the reason reads `path:archive`; a note that also declares `status: archived` is reported by its declaration). A retired memory leaves the digest even if it still sits under `decisions/` — it stays on disk, in git and searchable on demand. A memory with no `status` at all is *unmarked*: it is used as before and is not silently promoted to "active". A usable row also keeps its qualifiers: `contradicts` (an open conflict), `stale_backing` (a source it rested on was retired) and a declared `epistemic` marker travel with the row — as JSON keys on `POST /context/prime` and as `[⚠ contradicts: … | ⚠ stale backing: … | epistemic: …]` on the `palinode_session_init` and `palinode prime` text — so a contested or unverified snapshot cannot become an unqualified summary. An absent `epistemic` stays unmarked. "Recent" means the memory's effective date — its declared `date`, else the `last_updated` / `created_at` the save path stamps; touching a file is not a new effective decision, and undated memories rank last.

### Phase 2: Topic-Specific Search (per message)

After core injection, Palinode searches for context relevant to **what you just said**.

- Uses hybrid search: BM25 keyword matching + BGE-M3 vector similarity + RRF merge. In
  practice the arms split the work by query shape: a full-sentence question is retrieved
  almost entirely by the vector arm (FTS5's implicit AND requires every query token to
  co-occur in a document, which natural-language questions rarely satisfy), while BM25
  carries exact terms and identifiers that embeddings blur. The combined system measures
  0.981 evidence recall@10 on LongMemEval_S — see [BENCHMARKS](BENCHMARKS.md). If the
  keyword arm errors, search degrades to vector-only and logs a warning rather than failing.
- **Each arm is bounded against its own best match, before the merge.** A
  keyword candidate needs `search.fts_threshold` (default `0.4`) of the best
  normalized BM25 score in its candidate set; a vector candidate needs
  `search.vector_relative_floor` (default `0.85`) of the best cosine in its
  own. `search.mcp_threshold` / `search.api_threshold` remain the vector arm's
  absolute cosine floors and are unchanged: an absolute floor says whether a
  match is plausible at all, a relative one whether it is plausible beside the
  match this query actually found — and it is the second question that decides
  whether the rest of `limit` gets filled with weak neighbours. Both relative
  floors measure against the best candidate in their own arm, so the top match
  always survives: a search that has any match never returns nothing, and
  abstention stays a separate decision. Set either to `0.0` to restore the
  unbounded arm.
- Results are adjusted by **Temporal Decay**, bumping up scores for recently updated and highly important memories.
- Returns top 5 results, 700 chars each
- **Skipped for trivial messages** (< 15 chars, or acks like "ok", "sure", "thanks")
- Searches across all memory types: projects, decisions, insights, daily notes, research
- **Three provenance answers per hit, kept apart.** Every search result carries
  `freshness`, `span_integrity` and `currency`, each answering one question:
  - `freshness` — **index/source agreement**: does the stored section hash still match
    the file on disk? `valid` | `stale` | `unknown` (no stored hash). This is all a hash
    can say. The comparand is the *raw* section: a file that carries a superseded fact's
    `~~struck~~ [superseded …]` tombstone is `valid` — the index faithfully reflects the
    file even though its derived text projects the tombstone out (see
    [Current-Text Projection](#current-text-projection)) — so the label reads
    `[index matches source]`, never "verified" or "current".
  - `span_integrity` — **cited-span integrity**: are the record's `sources:` quote anchors
    ([§7d](#7d-source-citation-anchors-quote_hash)) still present verbatim in the files
    they cite? `unanchored` when it cites nothing, else `ok` or the worst anchor status
    (`anchor_tampered` | `source_drifted` | `source_missing`) — the same check
    `palinode_blame` runs.
  - `currency` — **assertion currency**: is the assertion still in force? Decided by the
    lifecycle classifier on the file's *live* frontmatter plus the chunk text: `retired`
    (archived / deprecated / superseded / retracted / expired, or the chunk carries a
    retired fact tombstone), `contested` (an open `contradicts` conflict), `current`
    (a live `status` declared), or `unmarked` (nothing declared — not promoted to
    current). `currency_reason` names the deciding signal (`status:archived`,
    `superseded_by: …`, `retired fact text`, `contradicts: …`).

  Renderers show them separately and only warn when there is something to warn about:
  `[index matches source]` / `[⚠ index stale]`, `[⚠ cited quote: source_drifted]`,
  `[⚠ retired: …]` / `[⚠ contested]` (the `⚠ contradicts: …` label already names the
  refs). `current` and `unmarked` are not labelled, matching the session digest.
  Compatibility: `freshness` keeps its field name, values and meaning; the other keys
  are additive; nothing about stored hashes or the index changes.
- **Tiered views** — `read` and `search` take a `tier` of `abstract`, `overview`, or
  `full` on every surface (CLI `--tier`, MCP `tier`, REST `tier`). `abstract` is the
  file's `summary:` frontmatter, falling back to `canonical_question:` and then the
  first paragraph, capped at ~300 characters — enough to decide whether a hit is worth
  opening. `overview` is the frontmatter block plus the head of the body, capped at
  4,000 characters (`read.abstract_max_chars` / `read.overview_max_chars` in
  `palinode.config.yaml`). `full` is the whole record. Tiers are computed
  deterministically at read time from content already in hand — no LLM, and no second
  content store: the markdown file remains the only place content lives. Omitting
  `tier` returns exactly what the surface returned before tiers existed.
- **Bounded evidence resolution** — `search` takes an opt-in `resolve` on every
  surface (CLI `--resolve`, MCP / REST / plugin `resolve`; `none` by default, which
  leaves the response byte-identical). A hit is a starting point: the record may have
  been replaced, may be in an open conflict, or may have been corrected by a record
  that ranked below the top-k or uses different words. `linked` follows each hit's
  `superseded_by`, `contradicts` and `backed_by` **forward and in reverse** — reverse
  edges come from the frontmatter the indexer stored (`chunks.metadata`; a
  `json_each` pass, not a directory scan) and are confirmed against the live file —
  under separate, deterministic budgets: `max_files` / `max_edges` / `max_depth` for
  link traversal and `max_replacement_chain` for `superseded_by` lineages, with cycle
  detection (`search.evidence` in `palinode.config.yaml`). Linked evidence bypasses
  the relevance floor and is attached to its seed's `evidence` block, so truncating
  the hit list can never keep a stale record and drop what corrects it. `full` adds
  bounded **unlinked discovery** under its own `fallback_max_queries` /
  `fallback_max_reads`: exact lookups first (the hit's `entities` against the entity
  index, its `sources` / `claims` refs), then a keyword query on its identifiers and a
  neighbour query on its own stored vector. Discovery never writes a link. Every
  expanded record passes the same path guard and visibility gate the hit passed and
  carries its own `currency`, `freshness` and a projected-text excerpt — a retired
  record's successor is surfaced without the retired wording being presented as
  current. Each hit's block carries `coverage`: `complete`, or `partial` with reasons
  from a closed vocabulary (`budget_exhausted:…`, `target_hidden`, `target_missing`,
  `scope_mismatch`, `index_lag`, `fallback_disabled`) that names no hidden record. A
  missing link or an exhausted budget is a reason, never a claim that no
  counterevidence exists. The resolver is read-only and decides nothing: which side
  of a conflict wins, and how it renders, is a separate layer.
- **The three outcomes** — with `resolve` on, each hit also carries a `resolution`
  block: exactly one of **`supported_current`** (a record stands, with the support
  behind it), **`unresolved_conflict`** (two or more sides that cannot both hold, all
  kept visible, no winner) or **`insufficient_evidence`** (unknown, said out loud).
  The decision is made once, server-side, and the CLI, MCP and REST surfaces only
  render it, so they cannot disagree about a hit. Only **mechanically explicit**
  changes resolve: a `superseded_by` chain that ends at a standing successor, a
  declared retirement (`archived` / `deprecated` / `superseded` / `retracted`), a
  past `expires_at`. Everything else is advisory — a `contradicts` link (including
  one consolidation proposed) can put a record on the list of sides, but a link, a
  newer date, an `epistemic: fact` label, a similarity score, a rank position and a
  recall count can none of them retire a record or pick a winner. So a newer
  *proposal* never replaces an accepted decision; a decision that disagrees with an
  observation of the running system is reported as a
  `policy_implementation_mismatch` rather than silently settled; two observations
  that cannot both hold stay contested; and when a replacement is itself withdrawn
  the answer is `insufficient_evidence`, never the value it replaced. A replacement
  the writer **dated forward** has not happened yet: until that date the record it
  will replace is still the answer (`replacement_scheduled`), carrying
  `superseded_from:<date>` so nobody is handed a value that is about to change
  without being told when — and only when that pending supersession is the whole of
  its retirement. A predecessor that also lapsed, was retracted or deprecated on its
  own account, or sits under `archive/` stays `insufficient_evidence`. Claims are
  classified from the existing vocabulary only (`type`, `epistemic`, `status`) into
  proposal / accepted intent / observation / inference, and a legacy record that
  declares none of them stays `unknown` and `undated` — nothing invents a date, a
  kind or an authority the record never claimed. Before two records are called a
  conflict they must overlap: different explicit `scope`, disjoint namespaced
  entities of the same kind (`env/production` vs `env/staging`) or non-overlapping
  time windows mean they coexist. Support is grouped by the origin it names — the
  same `claims[].source_id` + quote anchor, `sources[].ref` or `backed_by` ref — so
  one observation copied into a session summary, a snapshot and a decision
  rationale counts once and cannot outnumber a correction, while a record naming no
  anchor is reported as unknown lineage rather than as independent corroboration.
- **Support checks at read time** — retiring a source flags its direct dependents
  when the retirement fires. That leaves a source retired *since* the last
  maintenance pass unnoticed, and a conclusion two hops out unqualified. So every
  hit — and any replacement that could stand in its place — also gets a bounded
  check of what it rests on: each `backed_by` source, and each of *their* sources,
  to `search.evidence.max_support_hops` hops (default 2), one read per source, cycle
  terminating, charged against the same file budget, with
  `budget_exhausted:support_hops` in coverage when the walk stopped short. Findings
  arrive as `stale_backing:<ref>@<hop>:<reason>` qualifiers, and they distinguish
  three things: **withdrawn** support (the source was archived, superseded, expired,
  retired by location, or retracted with nothing named as falsifying it — the
  dependent is uncertain), a **disproven** conclusion (the source names
  `falsified_by`), and a source that merely **moved** (its current revision is not
  the one the record recorded as revalidated — a prompt to re-verify, not a
  withdrawal). A ref no file answers to is coverage, never a withdrawal; a source
  the requester may not see contributes nothing but `target_hidden`. Losing support
  yields `insufficient_evidence` — never the opposite claim, and never the value a
  retired source used to carry.
- **`backing_policy` and `revalidated`** — two optional frontmatter fields, both
  opt-in and both absence-is-neutral. A multi-source list means nothing in
  particular on its own, so the checker draws no automatic conclusion from one:
  `backing_policy: all-of` says the record stands only while every named source
  does, `any-of` while at least one does, and a list with neither is advisory — the
  findings are reported and the outcome changes only when every source is gone.
  `revalidated: [{ref, revision, at}]` is the explicit form of "I re-checked this
  against its source": a backing counts as revalidated only when the recorded
  revision equals that source's *current* whole-file hash, so re-saving the
  dependent — or reformatting it — certifies nothing, and a receipt never revives a
  source that was retired. Recording a check as durable state goes through the
  existing marker/executor/git path and writes one `stale_backing` entry; no prose
  is ever rewritten and no replacement value is ever derived from a source's change.

### Phase 2b: Bounded Resolution (the per-turn answer)

Phase 2 returns hits. A hit is a starting point, not an answer: the record it
came from may have been replaced, may be one side of an open conflict, or may
rest on a source that was since retired. `POST /resolve` (`palinode_resolve`,
`palinode resolve`) closes that gap in one call — it takes the prompt, gathers
bounded evidence around each seed, runs the [resolution
policy](#phase-2-topic-specific-search-per-message) over it, and returns a
compact bundle: the assertions that **stand** (with currency, index freshness
and source revision), what **replaced** what, **conflicts** with every side
intact, what is explicitly **unknown**, and the **coverage** that says how
completely it looked. Deterministic templates — no model runs, so it works
with Ollama cold (it degrades to keyword seeds and reports
`degraded:keyword_only`).

**Routing — three ways memory reaches a session, deliberately kept apart:**

| When | What runs | Budget |
|---|---|---|
| **Session start** | Ordinary priming: `/context/prime` + the core digest. No resolution — a startup digest is orientation, not an answer to a question nobody has asked yet. | Its own payload and char cap; the hook's own timeout (4 s default) |
| **Per turn** | Bounded resolution over the prompt: `POST /resolve`, injected as the rendered bundle. | **250 ms deadline** (`PALINODE_HOOK_RESOLVE_DEADLINE`), and the remaining injection budget |
| **Explicit follow-up** | `palinode_resolve` / `palinode_search` / `palinode_read` as tools, agent-initiated. | No deadline; not bounded by the per-turn budget |

Past the per-turn deadline the turn falls back to today's plain search hits —
**and says so**, with `resolution unavailable (deadline) — the memories below
are unresolved search hits…`. It never falls back silently: an unchecked hit
presented with the authority of a resolved answer is the failure the whole
operation exists to prevent. Switch the channel off with
`PALINODE_HOOK_RESOLVE=0` and the pre-resolution payload returns byte for
byte, marker included (there is none — nothing claimed resolution).

The same rule governs the output budget, and it is the **same packer** the
session-start digest uses (`palinode/core/packing.py`). When the budget bites,
whole units are dropped in priority order (standing assertions, then conflict
groups, then explicit unknowns, then replacements — history is the least
load-bearing thing in an answer about what stands); a conflict group is never
split, and one that cannot fit is reported by ref with
`budget_exhausted:conflicts` in the coverage line. Both caps are enforced:
`max_chars` on the rendered bundle, and estimated tokens
(`context.recall_max_tokens`), reported as `budget_exhausted:tokens` when that
is the one that bit. If there is not even room for the omission notice, the
channel injects nothing at all — silence is safe, half a conflict is not.

**Consumers do not slice the result.** The server packs the bundle to the room
the consumer passed in `max_chars`, and the plugin and the shipped hook both
trim only at a unit boundary: a block that does not fit is dropped whole and,
if it was a contested one, replaced by the same `⚠ N conflicts omitted for
budget — see …` stub the server emits. A final `text.slice(0, cap)` is exactly
how a conflict the server kept whole arrives with one side missing, so there
is no longer one anywhere on the path.

Every bundle also carries a **delivery receipt** (§ [10. Delivery
Receipts](#10-delivery-receipts-what-you-were-just-handed)): `receipt_ref` is
its id and `receipt` is the
public view — every record the bundle delivered, at the exact revision it was
delivered at, with its disposition, lineage and coverage. Building it writes
nothing: resolve records no retrieval event and no recall, receipt or not.

### Phase 3: Associative Context (Spreading Activation)

If your message discusses known entities (people, projects), Palinode searches the entity graph to find related files that share those entities. This surfaces horizontally related information that might not match exact keywords or semantic vectors, but is structurally related to the topic. Up to 3 related files are added.

### Phase 4: Prospective Triggers

Palinode maintains a background index of "triggers" (specific situational contexts). Every message is checked against this list. If the semantic meaning of your message matches a trigger description, the associated memory file is forcibly injected into the context. This allows the agent to essentially leave a "note to self" to remember a specific file the next time a specific situation arises.

A trigger acts under whatever authority existed when it was written, so it can carry an expiry: `palinode_trigger create` (MCP), `palinode trigger add --expires-at ... --authority ...` (CLI) and `POST /triggers` all accept `expires_at` (ISO-8601) and a free-text `authority`. An expired trigger is skipped at check time and logged once — not once per prompt — and the `archive-expired` sweep that ages out ephemeral memories also flips it to `enabled: 0`, so `palinode trigger list` shows the lapse. A trigger without `expires_at` never expires, exactly as before.

**What the agent sees (wrapped in `<palinode-memory>` tags):**

```xml
<palinode-memory>
## Core Memory

--- people/alice.md ---
> Alice Chen — Product lead at Acme Corp. Prefers async communication. Working on the mobile checkout redesign.

--- projects/checkout.md ---
> Mobile checkout redesign. v2 on React Native...

## Relevant Context
[decisions] My App will use 5 modules instead of 3. Alice's design direction...
[insights] For model fine-tuning, 90 curated samples outperform 1,623 raw...
</palinode-memory>
```text

### Sensitive Content Scrubbing

Before injection, all content passes through `specs/scrub-patterns.yaml` — regex patterns that redact credentials, phone numbers, and other PII. The agent never sees raw secrets.

---

## 2. Session Capture (End of Every Turn)

**Hook:** `agent_end` in the OpenClaw plugin

After each agent response, the plugin captures the conversation to a daily note:

1. Takes the last 10 messages (user + assistant)
2. Strips any `<palinode-memory>` tags (avoids feedback loop — don't store the injection itself)
3. Caps at 2,000 characters
4. Appends to `daily/YYYY-MM-DD.md`

**What a daily note looks like:**

```markdown
## Session 2026-03-29T04:26:16Z

user: what's the status on Palinode?

assistant: Search improvements are live. Consolidation cron enabled, entity linking live,
temporal search working. 2,165 chunks indexed across 219 files...
```text

Daily notes are **raw session logs** — unprocessed, append-only. They accumulate throughout the week and are distilled by the consolidation cron.

### Context Reset Capture

**Hook:** `before_reset`

When you run `/new` (session reset), the plugin flushes the last 20 messages to daily notes before the context is cleared. This ensures nothing is lost during a reset.

---

## 3. Quick Capture (`-es` flag)

**Hook:** `message_received`

Append `-es` to any message to save it directly to Palinode:

```text
Alice wants async check-ins instead of meetings -es
```text

**Smart routing by content type:**

| Input | Detected As | Destination |
| --- | --- | --- |
| `https://example.com -es` | Bare URL | Fetches page → `research/` |
| Long text (>500 chars) `-es` | Article/notes | `research/` as ResearchRef |
| Short text + URL `-es` | Note with source | Saves note + fetches URL |
| Short text `-es` | Quick fact | `insights/` |

**Safety checks:**

- Won't fire inside code blocks
- Requires word boundary (won't match mid-word)
- Minimum 5 characters
- URL regex strips trailing punctuation

**Receipt:** On the next turn, Palinode injects `*[Saved to Palinode: {slug}]*` into the context so the agent can acknowledge the capture naturally.

---

## 4. Weekly Consolidation (Sunday 3am UTC)

**Script:** `palinode/consolidation/runner.py`  
**LLM:** OLMo 3.1:32b via Ollama (localhost:11434)
**Schedule:** `0 3 * * 0` (crontab) — an upper bound, not the trigger
**Prompt:** `specs/prompts/consolidation.md`

The consolidation cron is where raw daily logs become curated memory.

The crontab entry decides how often the pass may be *considered*; the activity
gate decides whether it runs. A pass fires when at least 24 h have elapsed
**and** at least 5 sessions have been recorded since the last one — or when the
7-day ceiling passes, whichever comes first. So a busy week consolidates
mid-week and an idle one does not burn an LLM pass over nothing. Thresholds,
the ceiling, and how to turn the gate off are in
[OPERATIONS.md § Consolidation scheduling](OPERATIONS.md#consolidation-scheduling).

### What It Does

```mermaid
graph LR
    D[daily/*.md<br>7 days] --> COLLECT[Collect Notes]
    COLLECT --> GROUP[Group by Project<br>entity tags + keywords]
    GROUP --> LLM[OLMo 3.1<br>Distill per project]
    LLM --> WRITE[Update project summary]
    LLM --> DECISIONS[Detect superseded decisions]
    LLM --> INSIGHTS[Extract cross-project insights]
    D --> ARCHIVE[Move to archive/YYYY/]
```text

### Step by Step

1. **Collect** — reads all `daily/YYYY-MM-DD.md` files from the last 7 days
2. **Group** — assigns notes to projects by:
   - Entity tags in frontmatter (`entities: [project/my-app]`)
   - Keyword fallback (scans content for project names, tool names, etc.)
3. **Retire stale log lines** — before anything is sent to a model, each target's dated `- [YYYY-MM-DD] …` status log lines older than `consolidation.status_log_retention_days` (default 90; `0` disables) are archived into the `{name}-history.md` sibling. A date is arithmetic, not judgement, so this is deterministic — and it is what keeps step 5 tractable: a status document fed by session-end gains one line per session, and at several hundred lines an honest per-line proposal exceeds any token cap, is truncated, and retires nothing at all. Identity and profile documents are never swept — they retire by supersession, never by age. Reported as `age_retired`; the nightly pass does not do it
4. **Analyze** — for each project, sends notes + what is *left* of the summary + existing decisions to the LLM (OLMo 3.1:32b) with the compaction prompt to determine what facts are relevant
5. **Determine Operations** — the LLM returns a structured JSON array holding only the operations that *change* something (`UPDATE`, `MERGE`, `SUPERSEDE`, `ARCHIVE`, `ARCHIVE_BEFORE`, `RETRACT`, `PROPOSE_CONTRADICTS`). Any fact it does not name is kept as it stands, and an empty array means nothing needed changing — so the response size follows the number of judgments, not the size of the document. `ARCHIVE_BEFORE` is the bulk form of the same idea: one operation and one rationale retire every dated log line older than a given date
6. **Apply Changes** — modify or move the named facts:
   - Updated/Merged facts are preserved in the Identity or Status layers.
   - Superseded or Archived facts are moved to the History layer (`{name}-history.md`) with a rationale and timestamp ensuring data is never lost.
7. **Assign IDs** — any newly generated facts get a deterministic `<!-- fact:slug -->` ID block for tracking.
8. **Insights** — runs all notes (not per-project) through the insight extraction prompt, looking for cross-project patterns
9. **Archive** — moves to `archive/YYYY/` the daily notes whose every project group reached a decision (compacted, or saw the notes and proposed nothing). Notes stay in `daily/` — and the run summary says why — when a group failed or was skipped, when the note names no `project/` at all (it formed no group, so no pass read it), or when it is today's `daily/<date>.md`, which session-end is still appending to
10. **Commit** — `git commit -m "palinode: weekly consolidation {date}"`

### Token Budget

Each project compaction is capped at ~6,000 chars of daily notes (1,500 per note, most recent first). Combined with the existing structured facts, this fits comfortably within OLMo's 4,096 token context without losing detail.

### What It Produces

**Before consolidation (daily/2026-03-29.md):**

```text
## Session 2026-03-29T04:26:16Z
user: completed the latest test pass.
assistant: Updating My App with testing progress.

## Session 2026-03-29T16:12:25Z  
user: run the consolidation
assistant: Processed 18 notes, My App summary updated via 2 UPDATE, 1 ARCHIVE ops...
```text

**After consolidation (projects/my-app-status.md):**

```text
## Active Milestones
- <!-- fact:test-pass-complete --> [2026-03-29] Latest test pass completed successfully.
```text

**Archived directly to (projects/my-app-history.md):**

```text
## Archived Facts
- <!-- fact:test-pass-in-progress --> [2026-03-25] Working through the current test pass.
  *(Archived 2026-03-29: Superseded by m5-completed)*
```text

---

## 5. File Indexing (Continuous)

**Service:** `palinode-watcher` (systemd, watchdog library)

The file watcher monitors the entire memory directory. When any `.md` file is created, modified, or deleted:

1. **Parse** — split markdown into sections by heading
2. **Hash** — SHA-256 each section's raw content
3. **Project** — derive each section's *current text*: the executor's retirement
   tombstones (`~~old~~ [superseded YYYY-MM-DD]`, `~~old~~ [RETRACTED YYYY-MM-DD …]`,
   and the mention-level `[RETRACTED YYYY-MM-DD r:<id>].` span) are removed; everything
   else — ordinary `~~strikethrough~~`, code fences, inline code, malformed markers,
   frontmatter — is kept byte-for-byte. The projected text is hashed separately and
   stamped with the projection version.
4. **Skip if unchanged** — compare the raw hash and the projection stamp to the existing
   index entry
5. **Embed** — send the projected text to Ollama BGE-M3 (1024d vectors)
6. **Upsert** — store the projected text in SQLite-vec (vector) and FTS5 (keyword)
7. **Entity index** — extract `entities:` from frontmatter, update entity table

### Content-Hash Deduplication

Each chunk is hashed before embedding. If the hash matches the existing entry, the ~200ms Ollama API call is skipped entirely. On a full reindex of 2,000+ chunks where most are unchanged, this saves ~90% of embedding calls.

### Current-Text Projection

The executor never deletes a retired fact: `SUPERSEDE` and `RETRACT` strike it through in
place and leave the successor beside it, so the file shows its own history. Before this
projection the index was derived from that raw text, so the old wording still ranked in
BM25 and vector search and rendered in snippets beside the new one — and file-level
archive filtering could not help, because the file is not archived.

The index is now derived from a **projected current text**: the raw section is parsed and
hashed first (so section ids and `content_hash` are unchanged), then the executor's own
retirement renderings are removed, then the result is what both FTS5 and the embedder
see. Two hash domains live side by side and are never compared to each other:

| Column | Over | Used by |
| --- | --- | --- |
| `content_hash` | the raw section on disk | `freshness` (index/source agreement), `palinode_blame`, quote-anchor verification |
| `projected_hash` + `projection_version` | the derived current text | the reconcile planner, `palinode doctor` (`projection_current`) |

Nothing on disk changes. The retired wording is still in the raw file, in the
`-history.md` sidecar the executor appends to, in `git log`, and reachable through
`palinode_blame` / `palinode read` — it is simply no longer what search is built from. A
store indexed before the projection existed converges as its files are visited (a save,
the watcher, or `palinode reindex`): a chunk whose stored text already equals its
projection is stamped in place without re-embedding; one that still carries a tombstone
is re-derived. `palinode doctor` reports how many chunks are still behind.

### What Gets Indexed

| Directory | Indexed | Why |
| --- | --- | --- |
| `people/` | ✅ | Person memory |
| `projects/` | ✅ | Project snapshots |
| `decisions/` | ✅ | ADRs and choices |
| `insights/` | ✅ | Lessons learned |
| `daily/` | ✅ | Session logs |
| `research/` | ✅ | Reference material |
| `archive/` | ❌ | Processed, excluded |
| `inbox/processed/` | ❌ | Processed drops |
| `specs/` | ❌ | The store's own consolidation prompts — config you edit, not memory |
| `.git/` | ❌ | Git internals |
| `venv/`, `node_modules/` | ❌ | Build artifacts |

**What counts as memory:** `specs/`, `prompts/`, `logs/` and the store's dot-directories
are not memory on any surface — one predicate, matched against *every* directory segment
of a path, keeps them out of `/list` (and so the SessionStart injection), the provenance
UI, the advisory review, the session-start digest, `backed_by` propagation, and the
index. `daily/`, `archive/` and `inbox/` are per-surface decisions: the digest reads
`inbox/` because open action items live there, while the browse surfaces skip it.

---

## 6. Git Versioning (Every Change)

**Repo:** a dedicated Git repository for your memory directory

Every memory change is a git commit. This enables:

| Tool | What It Does | Example |
| --- | --- | --- |
| `palinode_diff` | What changed recently? | "Show me changes this week" |
| `palinode_blame` | When was this fact recorded? | "When did Alice mention async?" |
| `palinode_history` | How has this file evolved? | "Show My App's history" |
| `palinode_rollback` | Undo a bad change | "Revert last consolidation" |
| `palinode_push` | Sync to GitHub | "Backup my memory" |

### Auto-Commit Points

| Event | Commit Message |
| --- | --- |
| `palinode_save` | `palinode auto-save: {category}/{slug}.md` |
| `-es` capture | `palinode auto-save: {category}/{slug}.md` |
| Consolidation | `palinode: weekly consolidation {date}` |
| Description / summary backfill | `palinode backfill: N descriptions, M summaries (palinode {version})` |
| Rollback | `palinode: rollback {file} to {commit}` |
| Migration | `palinode: Mem0 backfill — N files from M memories` |

---

## 7. Entity Linking

Every memory file can reference entities via the `entities:` frontmatter field:

```yaml
entities: [person/alice, project/checkout]
```

The entity index is a reverse lookup: given an entity, find all files that mention it.

**API:** `GET /entities/person/alice` → returns all files referencing Alice

**Entity graph:** shows which entities co-occur. If `person/alice` and `project/checkout` always appear together, the system knows they're related.

**CLI:** `palinode entities` lists every tracked entity; `palinode entities person/alice` returns the files that reference it (see [CLI.md](CLI.md#palinode-entities)).

**Currently 20 entities tracked** across 219 files.

---

## 7a. External References

Memories can carry optional references to SDLC objects — GitLab MRs, issues, pipelines, GitHub PRs, Linear issues, Jira tickets, or any free-form key/value pair your workflow uses. These are stored in the `external_refs` frontmatter dict:

```yaml
---
id: decision-auth-approach
external_refs:
  gitlab_mr: "myorg/myrepo!42"
  gitlab_issue: "myorg/myrepo#17"
  github_pr: "phasespace-labs/palinode#99"
  linear_issue: "PAL-42"
  jira_issue: "PROJ-100"
---
```

**There are no live queries** — Palinode does not fetch or validate these references. They are pass-through metadata that travels with the memory and appears in search results. Recognised keys (`gitlab_mr`, `gitlab_issue`, `gitlab_pipeline`, `github_pr`, `linear_issue`, `jira_issue`) render with short pretty labels in `palinode_search` output; unrecognised keys pass through as-is.

**At save time:**
- API: `POST /save` with `external_refs: {"gitlab_mr": "palinode!42"}`
- CLI: `palinode save "..." --external-ref gitlab_mr=palinode!42 --external-ref linear_issue=PAL-1`
- MCP: `palinode_save` with `external_refs={"gitlab_mr": "palinode!42"}`

Nested values (dicts or lists) are soft-warned and dropped — every value must be a plain string.

---

## 7b. The Wiki Maintenance Contract

Each memory has two cross-reference channels: the typed `entities:` list in YAML frontmatter, and `[[wikilinks]]` in the note body. Both are first-class. The frontmatter is authoritative for the index and for entity-graph queries; the body links are what makes a note useful when read directly or rendered in Obsidian.

The LLM is the maintainer of consistency between the two surfaces. PROGRAM.md's "Wiki Maintenance" section is the canonical contract: when creating or updating a memory, decide what entities are referenced, add them in canonical `kind/slug` form to `entities:`, and either write the link inline in the body where it's load-bearing or let `palinode_save` materialise an idempotent `## See also` footer (delimited by `<!-- palinode-auto-footer -->`) for whatever doesn't have an inline anchor.

This contract is what makes Palinode's storage interoperable with Obsidian's graph view, with `palinode_orphan_repair`, with `palinode_dedup_suggest`, and with the entity-graph spreading-activation phase of recall above. Lint includes a `wiki_drift` check that warns when the two surfaces diverge. See [OBSIDIAN.md](OBSIDIAN.md#the-wiki-contract) for the user-facing summary.

---

## 7c. IETF KU Frontmatter Fields

Palinode memory files optionally carry three fields from the [IETF Knowledge Unit draft](https://datatracker.ietf.org/doc/draft-farley-acta-knowledge-units/) (`draft-farley-acta-knowledge-units`). These fields are purely additive — existing files without them continue to work unchanged.

| Field | Type | Meaning |
|---|---|---|
| `ku_version` | string (`"1.0"`) | Marks the file as KU-aligned at this draft revision. |
| `confidence` | float (0.0–1.0) | Author's or system's confidence in the memory content. Surfaced as a top-level key in search results. |
| `priority` | integer (1–5) | Human-assigned recall priority. Missing means normal (`3`). It adds only a small bounded ranking nudge and is separate from the system-assigned ADR-007 `importance` decay float. |
| `lifecycle` | `"active"` \| `"archived"` \| `"deprecated"` | Mirrors Palinode's existing `status` field in KU vocabulary. When auto-populated it copies `status` if that maps; otherwise `"active"`. |

**Auto-population:** Set `ku_compat: enabled: true` in `palinode.config.yaml` to automatically write `ku_version` and `lifecycle` on every save. These fields are written for interoperability only — Palinode itself never reads them back.

## 7d. Source Citation Anchors (`quote_hash`)

A memory can cite an exact passage from another file with a `sources:` frontmatter entry:

```yaml
sources:
  - ref: research/some-paper.md
    quote: "The exact cited passage."
    quote_hash: "sha256:<64-hex-digest>"
```

`quote_hash` uses the explicit `<algorithm>:<hex>` format. New hashes use `sha256`; `md5` is also understood for older anchors. For compatibility, Palinode resolves a bare 32-character hexadecimal digest as MD5 and a bare 64-character digest as SHA-256, but hand-written anchors should include the algorithm prefix.

The digest is computed from the normalized quote: smart punctuation is folded to its ASCII equivalent, each run of whitespace is collapsed to one space, and leading or trailing whitespace is removed. These smart-punctuation and spacing differences therefore do not invalidate an otherwise identical quote.

The `quote_hash` field may be omitted when saving through Palinode. Palinode computes and stores the default `sha256:` hash automatically. If a hash is supplied, it must match the normalized `quote`; a mismatched value is rejected as an inconsistent anchor. This hash checks citation integrity and is not a cryptographic signature or attestation.

On save, a valid legacy `md5:` or bare 32-character MD5 anchor is validated with MD5 and then stored as a `sha256:` anchor, upgrading it in place.

## 7e. Dependency Frontmatter (ProjectSnapshot)

ProjectSnapshot memories can carry optional dependency fields that model milestone and task sequencing:

```yaml
---
type: ProjectSnapshot
id: snapshot-m1-doctor
slug: milestone/M1.1-init
depends_on:
  - milestone/M1.0-bootstrap
blocks:
  - milestone/M2-agent-intelligence
parallel_with:
  - milestone/M4-import
status: in_progress  # in_progress | done | blocked
---
```

**`palinode_depends` tool:** given a slug, returns the dependency neighbourhood plus `unblocked: bool` (true when every `depends_on` is `status: done`) and `orphans` (referenced slugs that have no matching memory file).

**`--unblocked` mode:** `palinode depends --unblocked` (CLI) or `GET /depends/_unblocked` (API) returns all slugs whose dependencies are all done — answering "what can I work on right now?"

---

## 8. Behavioral Spec: PROGRAM.md

`PROGRAM.md` is the behavioral specification for the memory manager. It controls:

- What to extract (and what to ignore)
- Aggressiveness thresholds
- Consolidation rules
- Quality standards

The consolidation runner uses the prompt in `specs/prompts/compaction.md`. To change consolidation behavior, edit that file — no code changes needed. (PROGRAM.md documents overall agent behavior, not the consolidation runner specifically.)

That file lives in your **memory store**, not in the installed package.
`palinode init` puts it there, copied from the prompts that ship inside
palinode, and never overwrites an existing one — so an edit survives every
re-run of `init`, with or without `--force`. If the store has no copy, the
runner reads the packaged one and logs that it did; nothing silently skips.

The flip side of owning the file: a palinode release that improves a prompt
does not reach you until you take it. `palinode doctor` flags the gap
(`prompts_current`) and `palinode prompt sync` closes it — it replaces only the
copies that still match a version palinode shipped, and reports the ones you
have edited instead of overwriting them.

---

## Summary: What Happens When

| Event | What Palinode Does |
| --- | --- |
| **You send a message** | Injects core memory + topic-relevant context |
| **Agent responds** | Captures last 10 messages to daily notes |
| **You type `-es`** | Saves immediately, routes by content type |
| **You call `palinode_save`** | Writes typed markdown file, auto-commits |
| **A file is saved** | Watcher embeds + indexes it |
| **Sunday 3am** | Consolidation distills weekly notes into summaries |
| **You ask "what changed?"** | `palinode_diff` shows git diff of memory files |
| **You ask "when did I learn X?"** | `palinode_blame` traces to the commit |

---

## 9. Memory Provenance (The Git Chain)

Every fact in Palinode has a traceable origin. Here's how a memory evolves:

```text
Feb 11  — Mem0 auto-captured: "pagination limit reduces API response time"
          (Qdrant, no provenance, no context)

Mar 29  — Backfilled to Palinode: classified by Qwen 72B, written to
          projects/my-app-milestones.md (commit dcdbf5f)

Apr 06  — Weekly consolidation: OLMo distilled 7 days of session notes,
          updated my-app.md with new status bullets (commit abc1234)

Apr 13  — Manual edit: Alice corrected a fact via palinode_save (commit def5678)
```text

**`palinode_blame`** shows both the git date AND the true origin date:

```text
## Blame: projects/my-app-milestones.md
Origin: 2026-02-11 | Source: mem0-backfill
Note: Git shows 2026-03-29 (migration date). True origin is 2026-02-11 (from mem0-backfill).

^dcdbf5f (2026-03-29) - [2026-02-11] Deployment milestone completed across all modules
^dcdbf5f (2026-03-29) - [2026-02-15] Authentication and notifications are complete
abc1234  (2026-04-06) - Routing spec is ready
def5678  (2026-04-13) - Real-time sync is live
```text

For backfilled memories, git blame shows when the file was migrated. The frontmatter `created_at` field preserves the true origin date from the source system (Mem0, QC MCP, etc.). Palinode surfaces both so you always know:

- **When the fact was first captured** (frontmatter `created_at`)
- **When this file was last modified** (git blame date)
- **Where the memory came from** (frontmatter `source`)

For memories captured natively by Palinode (not backfilled), both dates match.

**`git log --follow`** shows the complete history of a file — every consolidation, every manual edit, every backfill.

### Backfill Provenance

Palinode has already absorbed memories from two external systems:

| Source | Memories | Classified By | Status |
| --- | --- | --- | --- |
| **Mem0** (Qdrant) | 4,637 → 3,645 (after dedup + skip) | Qwen 72B | ✅ Done |
| **QC MCP** (PostgreSQL) | 14,000+ contexts | TBD | Planned |

Backfilled memories enter your Palinode memory repository with `source: "mem0-backfill"` in their frontmatter. As consolidation updates them, each change gets its own commit and an audit trail builds over time. (The one-off Mem0 importer itself — `palinode migrate-mem0` / `POST /migrate/mem0` — was removed in v0.14.0; the provenance convention stays.)

### Why This Matters

Other memory systems are opaque databases. You can query them but you can't ask:

- "When did I first learn that Alice prefers async?" → `palinode_blame`
- "What changed about the My App project this week?" → `palinode_diff`
- "Show me every update to my infrastructure notes" → `palinode_history`
- "The last consolidation was bad, undo it" → `palinode_rollback`

These aren't add-on features. They're consequences of the architectural decision to use files + git as the source of truth. The audit trail is free.

## 10. Delivery Receipts (What You Were Just Handed)

Provenance so far answers *where did this memory come from*. A receipt answers the
other half: **what was handed to the agent just now, and at which exact revision.**

Every delivery — a search response, a `/context/prime` digest, a `/resolve`
bundle — produces one:

```text
Receipt: 4c1f0a92b6d7e310 · evaluated 2026-09-12T18:04:11+00:00
         · policy palinode/0.19.1+projection/1+config/bd38b7be
         · next transition 2026-09-15T00:00:00+00:00
  · decisions/db@7f21c0ab91de — replaced
  · decisions/db-v2@0a55e1c73b02 — selected
  · lineage source:observations/deploy-metrics — 2 records
    (projects/shop-snapshot, daily/2026-08-03) share one origin
  · coverage: partial (budget_exhausted:edges)
```

What each part is for:

| Field | Answers |
| --- | --- |
| `bundle_id` | The handle for this hand-off — the same id in the response, the retrieval log, and any later bundle |
| `policy_version` | Which selection/resolution policy served it (package + text projection + a fingerprint of the policy-relevant config) |
| `scope` | The caller scope the **server** resolved — never the caller's claim about itself |
| `evaluated_at` / `requested_time` | The clock the lifecycle policy used |
| `next_transition` | The nearest boundary that will change the answer — earliest `expires_at`, or a declared future `date`. `null` means *unknown*, never "never" |
| `supplied[]` | Every record handed over: ref, exact source revision, currency, freshness, span integrity, disposition, origin |
| `lineage[]` | Copies grouped by the origin they cite, at that origin's revision — three records citing one observation are one group, not three witnesses |
| `coverage` | How completely the evidence around the hits was gathered, and why it stopped |

Two properties worth stating plainly:

- **It records supplied context, not influence.** Nothing in a receipt claims a
  delivered record shaped what the agent did next. That edge is not built.
- **Unknown stays unknown.** A record whose revision was never computed says so; an
  unanchored record's lineage stays `unknown` rather than being counted as an
  independent observation; an unparseable date contributes no transition.

The receipt's **public view** — what every surface returns — carries refs, hashes and
dispositions, and no memory text (not even the query). An internal diagnostics view
adds the request and the reuse key for operators reading their own store.

Receipts are recorded on the retrieval log rows Palinode already wrote
(`.audit/retrievals.jsonl`), so `palinode trace <file>` can tell you which deliveries
a file was supplied in, at which revisions, under which disposition. Full field
reference, response shapes, and the cross-request reuse contract:
[`docs/DELIVERY-RECEIPTS.md`](DELIVERY-RECEIPTS.md).

## If something's wrong

Palinode's worst failure mode is *silent success* — every component reports healthy while serving wrong, stale, or orphan data (after a directory rename, an env-var drift, a watcher running with stale config, etc.). When search returns less than you expect or "save" feels like it didn't stick, run:

```bash
palinode doctor
```

Doctor is a read-only diagnostic command with 18 checks across path integrity, service health, config drift, index sanity, disk/backup, and a forward-looking CLAUDE.md scan. It surfaces the silent drifts that the rest of this document assumes never happen. Full guide: [`docs/DOCTOR.md`](DOCTOR.md).
