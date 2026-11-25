# PARITY.md — Cross-Surface Contract

**Status:** Active (ADR-010 accepted 2026-04-26)
**Source of truth:** `palinode/core/operations.py` for migrated operations; `palinode/core/parity.py` — the registry — for the rest
**Forcing function:** `tests/test_surface_parity.py` — CI-blocking
**Defaults:** the server's configuration for migrated operations; `palinode/core/defaults.py` for the rest

## The contract

Every memory operation that appears on more than one surface must use **the same canonical parameter names with the same shapes**. The three first-party surfaces are:

1. **CLI** — `palinode <command>`
2. **MCP** — `palinode_<tool>` (Claude Code, Cursor, IDEs)
3. **REST API** — `POST/GET /<endpoint>`

When you add a parameter, add it to all three surfaces (modulo exemptions below) and to `parity.py`. CI fails otherwise.

**Plugins are opt-in, not a fourth mandatory surface.** Under ADR-019 a plugin (OpenClaw today, Pi, and any future harness adapter) is a thin delivery adapter over the REST API — non-implementation of an operation is the expected case, not drift. An operation joins the plugin parity obligations only by setting `plugin_tool` in the registry; for those operations, the plugin must expose the same canonical params (enforced by `plugin/test/parity.test.ts`). New plugins add **no** registry obligations.

## Operation specs (generated)

Migrated operations are defined once, in `palinode/core/operations.py`. Their params are not restated in `parity.py`: its entry is derived from the spec. MCP's `inputSchema` properties and the MCP and CLI request bodies are generated from the spec, and the click options, the REST model and the plugin tool are checked against it (`tests/test_operation_spec.py`, `plugin/test/operation-spec.test.ts`). **The server owns defaults:** a surface sends only what the caller set.

To change one of these params, edit the spec, implement it on each surface, and regenerate the table below with `uv run python scripts/render-parity-doc.py`. A test fails while it is stale.

<!-- BEGIN GENERATED: operation specs (scripts/render-parity-doc.py) -->

### `search` — `/search`

| Param | Type | Enum | Required | Server default (out of the box) | Forwarding |
|---|---|---|---|---|---|
| `query` | string |  | yes |  | sent when set |
| `category` | string | `people`, `projects`, `decisions`, `insights`, `research` |  |  | not sent when empty |
| `limit` | integer |  |  | `search.default_limit` (15) | sent when set |
| `date_after` | string |  |  |  | not sent when empty |
| `date_before` | string |  |  |  | not sent when empty |
| `include_daily` | boolean |  |  |  | sent only when true |
| `include_telemetry` | boolean |  |  |  | sent only when true |
| `include_other_projects` | boolean |  |  |  | sent only when true |
| `since_days` | integer |  |  |  | sent when set |
| `types` | array of enum | `PersonMemory`, `Decision`, `ProjectSnapshot`, `Insight`, `ResearchRef`, `ActionItem` |  |  | not sent when empty |
| `where` | array of string |  |  |  | not sent when empty |
| `min_priority` | integer |  |  |  | sent when set |
| `threshold` | number |  |  | `search.mcp_threshold` (0.4) for MCP callers, else `search.api_threshold` (0.5) | sent when set |
| `tier` | string | `abstract`, `overview`, `full` |  |  | not sent when empty |
| `resolve` | string | `none`, `linked`, `full` |  |  | not sent when `none` |
| `include_retired` | boolean |  |  |  | sent only when true |

Display overrides (shown to a person; programs get the server default):

- **cli** `limit` = 3: A person reading a terminal. The server's 15 was measured for agent consumers (the k-sweep behind search.default_limit); JSON and piped output send nothing and get the server default.

### `list` — `/list`

| Param | Type | Enum | Required | Server default (out of the box) | Forwarding |
|---|---|---|---|---|---|
| `category` | string | `people`, `projects`, `decisions`, `insights`, `research` |  |  | not sent when empty |
| `core_only` | boolean |  |  |  | sent only when true |
| `where` | array of string |  |  |  | not sent when empty |
| `include_inbox` | boolean |  |  |  | sent only when true |
| `include_other_projects` | boolean |  |  |  | sent only when true |

### `read` — `/read`

| Param | Type | Enum | Required | Server default (out of the box) | Forwarding |
|---|---|---|---|---|---|
| `file_path` | string |  | yes |  | sent when set |
| `meta` | boolean |  |  |  | sent only when true |
| `tier` | string | `abstract`, `overview`, `full` |  |  | not sent when empty |

### `save` — `/save`

| Param | Type | Enum | Required | Server default (out of the box) | Forwarding |
|---|---|---|---|---|---|
| `content` | string |  | yes |  | sent when set |
| `type` | string | `PersonMemory`, `Decision`, `ProjectSnapshot`, `Insight`, `ResearchRef`, `ActionItem` | yes |  | sent when set |
| `slug` | string |  |  |  | not sent when empty |
| `path` | string |  |  |  | sent when set |
| `core` | boolean |  |  |  | sent when set |
| `entities` | array of string |  |  |  | sent when set |
| `project` | string |  |  |  | not sent when empty |
| `title` | string |  |  |  | not sent when empty |
| `metadata` | object |  |  |  | sent when set |
| `confidence` | number |  |  |  | sent when set |
| `priority` | integer |  |  |  | sent when set |
| `epistemic` | string | `fact`, `inference`, `open_question`, `unverified` |  |  | sent when set |
| `external_refs` | object |  |  |  | sent when set |
| `source` | string |  |  |  | not sent when empty |
| `update_policy` | string | `append`, `replace` |  |  | sent when set |
| `sources` | array of object |  |  |  | sent when set |
| `contradicts` | array of string |  |  |  | sent when set |
| `backed_by` | array of string |  |  |  | sent when set |
| `claims` | array of object |  |  |  | sent when set |

Shorthands (a surface fills a required param from its own sugar):

- **mcp, cli** `ps` → `type`: `ps` is shorthand for type=ProjectSnapshot; the surface sends the type.
- **cli** `--file` → `content`: `palinode save --file PATH` reads the content from a file.

### `session_end` — `/session-end`

| Param | Type | Enum | Required | Server default (out of the box) | Forwarding |
|---|---|---|---|---|---|
| `summary` | string |  | yes |  | sent when set |
| `decisions` | array of string |  |  |  | sent when set |
| `blockers` | array of string |  |  |  | sent when set |
| `project` | string |  |  |  | not sent when empty |
| `source` | string |  |  |  | not sent when empty |
| `push` | boolean |  |  |  | sent when set |
| `dry_run` | boolean |  |  |  | sent only when true |
| `harness` | string |  |  |  | not sent when empty; cli, api only |
| `cwd` | string |  |  |  | not sent when empty; cli, api only |
| `model` | string |  |  |  | not sent when empty; cli, api only |
| `trigger` | string |  |  |  | not sent when empty; cli, api only |
| `session_id` | string |  |  |  | not sent when empty; cli, api only |
| `duration_seconds` | integer |  |  |  | cli, api only |

Surface limits (a param only some surfaces advertise):

- `harness`, `cwd`, `model`, `trigger`, `session_id`, `duration_seconds` on cli, api only: Provenance a harness supplies about its own session: the session-end hook and `palinode session-end` fill it; an agent calling the MCP tool would have to invent it.

<!-- END GENERATED: operation specs -->

## Adding or changing a parameter

For an operation not yet in `operations.py`:

1. Add the `CanonicalParam` to the relevant `Operation` in `palinode/core/parity.py`.
2. If the parameter has a default that's shared across surfaces, add it to `palinode/core/defaults.py` and reference via `default_key`.
3. Implement on every required surface (see the Operation's `required_surfaces`).
4. Run `pytest tests/test_surface_parity.py`. If a surface is intentionally lagging — say, a CLI implementation will land in a follow-up PR — record it as `known_drift[("cli", "name")] = <issue_number>`. The test then xfails with the issue ref instead of failing.
5. Open the GitHub issue, link to the canonical param, and assign the missing surface to a follow-up.

When the surface is fixed, **remove the `known_drift` entry**. The parity test fails loudly if drift was tracked and the param now exists — that's the test telling you to close the issue.

### Drift or realization

A surface that does not expose a canonical param is not automatically lagging. Two mechanisms sit side by side on `Operation`, and picking the wrong one puts a false statement in the registry:

| | `known_drift` | `surface_realizations` |
|---|---|---|
| Says | the surface should expose this and does not, yet | the surface exposes this as its own capability |
| Value | the GitHub issue tracking the fix | the capability that realizes it, in the identifier form for that surface |
| Lifetime | temporary; removed when the surface is fixed | permanent; the arrangement is the design |
| Parity test | xfails, naming the issue | passes, naming the capability |
| Inventory guard | unaffected | counts the named capability as registered, so it needs no `INVENTORY_BACKLOG` row |

The test question is whether there is anything to fix. `depends` is the worked example: the API realizes the canonical `unblocked` boolean as `GET /depends/_unblocked` rather than as a query param on `GET /depends/{slug}`, and the CLI and MCP call that route. Nothing is lagging, so no issue number can ever be right for it, and it is declared as `surface_realizations[("api", "unblocked")] = "GET /depends/_unblocked"`.

`test_surface_realizations_name_a_real_param_and_capability` checks both halves of every declaration: the param exists in `canonical_params`, and the capability is live on that surface. A declaration nothing verifies rots — the entry this replaced carried an issue number from a private tracker, which resolved in the public one to an unrelated closed issue, and the parity test reported that number for months.

## Context resolution and startup gates

`core.context_prime.resolve_context` owns project selection. Explicit project
arguments bypass the local resolver's `context.enabled`; ambient environment,
mappings and directory inference obey it. The API's `effective_project_scope`
additionally enforces the server's own setting on search, prime, resolve, read
and trigger delivery: with context disabled, no caller-supplied project is applied. CLI/MCP
present the server's recall results; session-end consumes the resolver's project
view. Save tags only the project/entities explicitly supplied.
[Project resolution](HOW-MEMORY-WORKS.md#choosing-the-project) documents precedence,
git/worktree identity, remote-path limits and known/unrecognized diagnostics.

Startup delivery has an intentional gate difference: MCP `palinode_session_init`
honors `auto_inject.enabled` and per-harness suppression to avoid duplicate startup
injection. Direct `POST /context/prime` and `palinode prime` remain available for
hooks and deliberate inspection. These gates do not disable search or capture.
`tests/test_project_resolution.py` tests the behavioral contract in addition to
the parameter-name checks in `tests/test_surface_parity.py`.

## Admin-exempt operations

These operations are **not** required to appear on every surface. They are intentionally CLI-only or CLI+API only because they're operational, not memory-semantic.

| Operation              | Surfaces                | Reason                              |
|------------------------|-------------------------|-------------------------------------|
| `reindex`              | CLI + API               | Full database rescan                |
| `rebuild-fts`          | CLI + API               | FTS5 index rebuild                  |
| `split-layers`         | CLI + API               | One-shot core-layer migration       |
| `bootstrap-fact-ids`   | CLI + API               | Backfill fact IDs                   |
| `migrate-openclaw`     | CLI + API               | One-off importer                    |
| `doctor`               | CLI                     | Local diagnostics                   |
| `start`, `stop`        | CLI                     | systemd unit control                |
| `config`, `banner`     | CLI                     | Local UI                            |
| `health`, `git-stats`  | API                     | Observability internals             |
| `generate-summaries`   | API                     | Internal LLM workflow               |

The list is canonical at `palinode.core.parity.ADMIN_EXEMPT_OPERATIONS`. Adding a new admin operation requires updating the table here and that constant. Adding a new memory operation does **not** put it on this list — memory operations are subject to parity by default.

## Canonical names — the high-friction list

These are the spellings the registry agreed on. Forbidden aliases are flagged in PR review (and grepped by `scripts/check-httpx-monopoly.sh` for one specific class — see below).

| Concept                         | Canonical            | Forbidden aliases                       |
|---------------------------------|----------------------|-----------------------------------------|
| Path to a memory file           | `file_path`          | `file`, `path`, `filename`              |
| Memory category (directory)     | `category` (plural)  | `category` (singular), `dir`            |
| Project association             | `project` (string)   | `project_slug`, `entity` prefix only    |
| Entity refs                     | `entities` (list)    | `entity` (single), `tags`               |
| Memory type (closed enum)       | `type`               | `kind`, `category`                      |
| ProjectSnapshot shortcut        | `ps` (boolean)       | `is_ps`, `snapshot`                     |
| Dry-run preview flag            | `dry_run`            | `--execute` (negation), `preview`       |
| Human recall priority           | `priority` (1–5)     | `importance` frontmatter/API field      |
| Minimum recall priority filter  | `min_priority`       | `min_importance`                        |
| Source-surface attribution      | `X-Palinode-Source` header (preferred) or `source` field | per-surface `source` literals |

**`path` on `save` is the one place `path` is canonical.** It names the file a save
will create, relative to the memory dir, where `file_path` names a memory that
already exists. It is the same parameter, spelled `path` or `--path`, on every
surface.

**`--execute` means one thing: apply mode on a CLI-only maintenance command.**
`repair-status`, `worktree-reconcile`, and `migrate frontmatter` are dry-run by
default and take `--execute` to write; they are admin-exempt and have no
`dry_run` parameter on any other surface, so there is nothing for the flag to
alias. A registered memory operation that previews (`rollback`, `consolidate`,
`archive_expired`) spells the switch `dry_run` on every surface (`--dry-run`,
or the `--dry-run/--no-dry-run` pair where the default is a preview, on the
CLI); `--execute` as its negation is the forbidden alias above, and the last
one (`rollback --execute`) has been removed.

### Categories — exact set

Memory categories match directory names (plural), per `palinode/core/memory_write.py:_MEMORY_CATEGORY_DIRS`:

```
people, projects, decisions, insights, research
```

Singular variants (`person`, `project`, etc.) are **entity-ref prefixes**, not category values — see `palinode/core/memory_write.py:_CATEGORY_TO_ENTITY_PREFIX`.

### Memory types — exact set

```
PersonMemory, Decision, ProjectSnapshot, Insight, ResearchRef, ActionItem
```

Stored at `palinode/core/operations.py:MEMORY_TYPES`. The Python surfaces import
that tuple directly; the plugin mirrors it as a TypeBox literal union, with the
cross-language parity test guarding against drift.

### Prompt tasks — exact set

```
compaction, extraction, update, classification, nightly-consolidation
```

Stored at `palinode/core/operations.py:PROMPT_TASKS`. The ADR-010 parity pass fixed the duplicate-`enum` bug; the canonical list now lives in the operation spec.

## Surface sugar — opt-in convenience, not parity

A few parameters are surface-specific by design — they exist to make a surface ergonomic without changing the underlying API contract. These are **not** in the canonical params list. The plugin and other surfaces are free to add them or skip them; the parity test does not enforce.

- **`save --ps` / MCP `ps`** — shorthand for `type=ProjectSnapshot`. Resolved locally before the API call. The CLI and MCP have it; the API and plugin do not need it.
- **CLI `save --file <path>`** — read content from a file rather than passing inline. Local convenience; the API takes content directly.
- **CLI `save --importance N` / `--important` / `--critical`** — ergonomic aliases that map to canonical `priority` (`--important` = 4, `--critical` = 5). Do not expose human priority as API/frontmatter `importance`; that name remains the ADR-007 system demand-decay float.

If a surface adds sugar, document it here.

## How the test reads parity

`tests/test_surface_parity.py` walks `REGISTRY` and for each `(operation, surface, canonical_param)` tuple:

1. **Exempt surface?** Skipped (per `Operation.exempt_surfaces`).
2. **Plugin?** Skipped on the Python side (Python can't introspect the TypeBox schemas). The TS-side test at `plugin/test/parity.test.ts` enforces plugin parity using the JSON dump produced by `scripts/dump-parity-registry.py`. Run with `cd plugin && npm test`.
3. **In `known_drift`?** xfailed with `reason="drift tracked in #<issue>"`. The test passes; the issue tracks the fix.
4. **In `surface_realizations`?** Passed, naming the capability that realizes the param. Not drift, so not reported as drift.
5. **Otherwise:** asserted present. Missing → CI red.

The test additionally enforces:

- `test_admin_exempt_ops_are_not_in_registry` — the two lists are disjoint.
- `test_default_keys_resolve` — every `default_key` reference in the registry exists in `palinode/core/defaults.py`.
- `test_known_drift_references_a_canonical_param` — `known_drift` keys must reference real canonical param names (catches dangling drift entries after a refactor).
- `test_surface_realizations_name_a_real_param_and_capability` — `surface_realizations` keys must reference real canonical param names, and each value must name a capability that is live on that surface.

## Inventory completeness — the surface→registry direction

The param checks above walk `REGISTRY` and verify each surface (registry→surface). That direction is blind to the opposite failure: a **new capability shipped on a surface but never registered**. The param test only iterates operations it already knows about, so an unregistered tool/route/command stays invisible to the contract.

`test_no_unregistered_capabilities` closes the gap. It enumerates the **live** capabilities of each surface — MCP `list_tools()`, the FastAPI `app.routes`, the Click command tree — and asserts every one is accounted for by exactly one of:

1. **`REGISTRY`** — a parity-bound memory operation (mapped via its `mcp_tool` / `api_endpoint` / `cli_command`).
2. **`INVENTORY_INFRA`** (`palinode/core/parity.py`) — framework/admin/observability surface that is *not* a memory operation: Swagger/Redoc/OpenAPI, the HTML inspector under `/ui`, liveness probes, and the DB-maintenance + importer endpoints (the surface-identifier form of `ADMIN_EXEMPT_OPERATIONS`).
3. **`INVENTORY_BACKLOG`** (`palinode/core/parity.py`) — a memory-semantic operation that already ships on the surface but has **not yet** been promoted into `REGISTRY` with canonical params. Each entry maps to its tracking issue (the ADR-010 implementation backlog), and alternate names annotate the canonical entry instead of counting as additional capabilities. These are acknowledged, not silently ignored.

A capability named by `surface_realizations` counts as registered under (1): the operation declares that this is how the surface realizes one of its canonical params, so it is part of the registered contract rather than a backlog row awaiting promotion.

A live capability in none of the three buckets **fails the guard** — that is an operation that skipped the contract. Stale buckets also fail (`test_inventory_accounting_is_not_stale`): an entry whose capability was renamed or removed must be cleaned up, mirroring the `known_drift` hygiene rule. `test_inventory_buckets_are_disjoint` keeps each capability classified exactly once.

**Promoting a backlog op into the registry:** add its `Operation` (with canonical params) to `REGISTRY` and remove its `INVENTORY_BACKLOG` entry. The disjoint check fails if you register it without removing the backlog row — that is the test telling you the move is complete.

Identifier form per surface: MCP = tool name (`palinode_search`); API = `METHOD /path` (`POST /search`); CLI = command path (`trigger add`).

### Registration backlog — memory ops not yet in the registry

These memory-semantic operations ship on all of MCP/API/CLI today but are not yet promoted into `REGISTRY` with canonical params. They are tracked in the internal registration backlog (admin/framework surface is in `INVENTORY_INFRA`, not here):

`dedup_suggest`, `diff`, `entities`, `history`, `ingest`/`ingest-url`, `lint`, `orphan_repair`, `prompt` (list/show/activate), `push`, `session_end`, and the trigger `list`/`remove` + `check-triggers` + `search-associative` API endpoints.

Promoting each (registry `Operation` + canonical params + removing its backlog entry) is the per-op work the issue tracks; this contract makes the gap explicit and prevents *new* unregistered ops from slipping in alongside them.

## httpx monopoly — the bypass linter

Python CLI commands that call the Palinode API go through
`palinode/cli/_api.py`. Direct `httpx` calls from another CLI module skip rate
limiting, audit logging, source headers, and any future API-side fixes.

`scripts/check-httpx-monopoly.sh` enforces that boundary. It scans
`palinode/cli/*.py`, allows the canonical `_api.py` client, and fails on raw
`httpx` use anywhere else in that scope. Its `GRANDFATHERED` list is empty, so
there are no accepted bypasses to inventory here.

CI runs both the guard and its regression suite in the `httpx-monopoly` job:
`bash scripts/check-httpx-monopoly.sh` followed by
`bash tests/test_check_httpx_monopoly.sh`. The executable guard is the current
inventory; this document does not duplicate temporary exceptions.

## Known drift

`Operation.known_drift` in `palinode/core/parity.py` is the only drift
inventory. It records surfaces that are lagging, not surfaces that differ by
design — for those see `surface_realizations` and the table under "Drift or
realization" above. Do not copy its entries into a hand-maintained table here: that
second list cannot participate in either parity guard and will drift from the
registry again.

The Python suite validates the registry's Python-surface entries and rejects
dangling drift keys. `plugin/test/parity.test.ts` reads the same registry dump
and applies the same rule to plugin parameters. When a drift issue is fixed,
remove its `known_drift` entry and run both suites; each guard deliberately
fails if a now-present parameter still carries a stale exception.

## See also

- ADR-010 (the cross-surface parity contract) — the decision and rationale.
- `palinode/core/parity.py` — the registry (source of truth).
- `palinode/core/defaults.py` — shared defaults.
- `tests/test_surface_parity.py` — the forcing function.
