# PARITY.md — Cross-Surface Contract

**Status:** Active (ADR-010 accepted 2026-04-26)
**Source of truth:** `palinode/core/parity.py` — the registry
**Forcing function:** `tests/test_surface_parity.py` — CI-blocking
**Defaults:** `palinode/core/defaults.py` — single-place values for thresholds, cooldowns, source headers

## The contract

Every memory operation that appears on more than one surface must use **the same canonical parameter names with the same shapes**. The four surfaces are:

1. **CLI** — `palinode <command>`
2. **MCP** — `palinode_<tool>` (Claude Code, Cursor, IDEs)
3. **REST API** — `POST/GET /<endpoint>`
4. **OpenClaw plugin** — `palinode_<tool>` (TypeScript)

When you add a parameter, add it to all four surfaces (modulo exemptions below) and to `parity.py`. CI fails otherwise.

## Adding or changing a parameter

1. Add the `CanonicalParam` to the relevant `Operation` in `palinode/core/parity.py`.
2. If the parameter has a default that's shared across surfaces, add it to `palinode/core/defaults.py` and reference via `default_key`.
3. Implement on every required surface (see the Operation's `required_surfaces`).
4. Run `pytest tests/test_surface_parity.py`. If a surface is intentionally lagging — say, a CLI implementation will land in a follow-up PR — record it as `known_drift[("cli", "name")] = <issue_number>`. The test then xfails with the issue ref instead of failing.
5. Open the GitHub issue, link to the canonical param, and assign the missing surface to a follow-up.

When the surface is fixed, **remove the `known_drift` entry**. The parity test fails loudly if drift was tracked and the param now exists — that's the test telling you to close the issue.

## Admin-exempt operations

These operations are **not** required to appear on every surface. They are intentionally CLI-only or CLI+API only because they're operational, not memory-semantic.

| Operation              | Surfaces                | Reason                              |
|------------------------|-------------------------|-------------------------------------|
| `reindex`              | CLI + API               | Full database rescan                |
| `rebuild-fts`          | CLI + API               | FTS5 index rebuild                  |
| `split-layers`         | CLI + API               | One-shot core-layer migration       |
| `bootstrap-fact-ids`   | CLI + API               | Backfill fact IDs                   |
| `migrate-openclaw`     | CLI + API               | One-off importer                    |
| `migrate-mem0`         | CLI + API               | One-off importer                    |
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

### Categories — exact set

Memory categories match directory names (plural), per `palinode/api/server.py:660-668`:

```
people, projects, decisions, insights, research
```

Singular variants (`person`, `project`, etc.) are **entity-ref prefixes**, not category values — see `_CATEGORY_TO_ENTITY_PREFIX` in `server.py:180-187`.

### Memory types — exact set

```
PersonMemory, Decision, ProjectSnapshot, Insight, ResearchRef, ActionItem
```

Stored at `palinode/core/parity.py:MEMORY_TYPES`. The plugin's `palinode_save` schema declares `type` (it accepts the enum values as a free-form string today; tightening to a TypeBox `Union` of literal types is the remaining slice of #166).

### Prompt tasks — exact set

```
compaction, extraction, update, classification, nightly-consolidation
```

Stored at `palinode/core/parity.py:PROMPT_TASKS`. ADR-010 #162 fixed the duplicate-`enum` bug at `palinode/mcp.py:624-625`; the canonical list now lives in `parity.py`.

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
4. **Otherwise:** asserted present. Missing → CI red.

The test additionally enforces:

- `test_admin_exempt_ops_are_not_in_registry` — the two lists are disjoint.
- `test_default_keys_resolve` — every `default_key` reference in the registry exists in `palinode/core/defaults.py`.
- `test_known_drift_references_a_canonical_param` — `known_drift` keys must reference real canonical param names (catches dangling drift entries after a refactor).

## httpx monopoly — the bypass linter

CLI commands and the plugin go through one HTTP layer each: `palinode/cli/_api.py` and `palinode/mcp.py`. Direct `httpx` calls from elsewhere skip rate limiting, audit logging, source headers, and any future API-side fixes. The pre-commit linter at `scripts/check-httpx-monopoly.sh` greps for offenders and fails CI.

Today's bypass-vector files (cleanup tracked in #168 and #170 lower-tier):

- `palinode/cli/read.py` — reads disk directly, never calls API
- `palinode/cli/list.py` — uses raw `httpx.get`
- `palinode/cli/lint.py` — falls back to direct module import on connection failure
- `palinode/cli/session_end.py` — bypasses `_api.py`

After cleanup, the linter prevents recurrence.

## Known drift — at-a-glance

The registry is the precise list. This summary tracks roll-up status:

| Issue | Operation | Surfaces | Param           | Status |
|-------|-----------|----------|-----------------|--------|
| #159  | save      | CLI/MCP/API/Plugin | project | open |
| #161  | search    | MCP      | category enum (singular→plural) | **fixed in this commit** |
| #162  | prompt    | MCP      | duplicate `enum` keys           | **fixed in this commit** |
| #163  | search    | CLI      | since_days, types, threshold, date_after, date_before, include_daily | open |
| #163  | search    | MCP      | since_days, types, threshold | open |
| #163  | search    | Plugin   | threshold, since_days, types, date_after, date_before, include_daily | open |
| #164  | rollback  | MCP      | file_path (canonical name)      | open |
| #164  | rollback  | CLI      | dry_run (--execute negation)    | open |
| #164  | blame     | MCP      | file_path (canonical name)      | open |
| #165  | trigger.create | CLI | cooldown_hours, trigger_id     | open |
| #165  | trigger.create | MCP | threshold, cooldown_hours      | open |
| #166  | save      | CLI/MCP  | metadata, confidence            | open |
| #166  | save      | CLI      | slug, core                      | open |
| #166  | save      | MCP/API  | title                           | open |
| #166  | save      | Plugin   | metadata, confidence, project, title, source | open |
| #168  | read      | MCP      | meta (frontmatter passthrough)  | open |
| #169  | consolidate | MCP    | dry_run, nightly                | open |

When an issue closes:
1. Remove its entry from `Operation.known_drift` in `palinode/core/parity.py`.
2. Update the row in this table.
3. Run the parity test — should go from xfail to pass for the affected case.

## See also

- `ADR-010-cross-surface-parity-contract.md` — the decision and rationale.
- `palinode/core/parity.py` — the registry (source of truth).
- `palinode/core/defaults.py` — shared defaults.
- `tests/test_surface_parity.py` — the forcing function.
- Issue #170 — implementation tracking.
