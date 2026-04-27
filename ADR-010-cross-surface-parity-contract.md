# ADR-010: Cross-Surface Parity Contract

**Status:** Implemented (Python surfaces — CLI, MCP, API)
**Date:** 2026-04-26
**Context:** Drift audit (issue #159 precipitator), reinforces ADR-001
**Implementation:** all 8 audit findings closed on `feat/adr-010-surface-parity`
(#159, #161, #162, #163, #164, #165, #166, #167, #168, #169). Plugin TS-side
parity is the remaining v0.6 work (4 known_drift entries flagged plugin-only;
test skips plugin in v0).

## Decision

The implicit "all four interfaces expose the same capabilities" rule from `CLAUDE.md` is promoted to a **load-bearing, testable contract** with three artifacts:

1. **`docs/PARITY.md`** — the canonical-names registry. One operation, one name, one parameter spelling per concept (e.g. `file_path` not `file`, `category` plural matching directory names, `project` is its own first-class param not a hidden `entity` prefix). Includes an explicit **exemption list** for admin-only operations.
2. **`tests/test_surface_parity.py`** — a CI-blocking test that walks the live registries (FastAPI `app.routes`, MCP `list_tools()`, the click registry, the plugin's exported tools) and asserts equivalence per the registry. Adding a parameter to one surface without updating the others fails CI.
3. **A linter rule** (pre-commit) banning `httpx` calls outside `palinode/cli/_api.py` and `palinode/mcp.py`. CLI commands that bypass the HTTP layer are how we got `cli/read.py`, `cli/list.py`, `cli/lint.py`, and `cli/session_end.py` doing their own thing — every bypass is a parity-rot vector.

The four surfaces (MCP, REST API, CLI, OpenClaw plugin) remain. We do **not** generate them from a single source of truth in this ADR (deferred — see Consequences).

## Context

ADR-001 declared the **tools are the interface**: 17 MCP tools + a memory directory, not a 4-phase injection pipeline. That decision makes parity load-bearing — the contract surface is what survives model changes, and the contract surface is plural.

In practice, the surfaces have drifted. A 2026-04-26 audit (driven by Codex's report in #159 that `palinode save --project palinode` was rejected) found ten distinct drift patterns. Highlights:

- **#159 already filed:** `save` accepts no `--project` despite `session-end` doing so on every surface.
- **Silently-wrong category enum:** `palinode_search` MCP tool uses singulars (`person`, `project`, …) while the store filters by directory name (`people/`, `projects/`, …). MCP callers passing `category="person"` get zero hits with no error. (`palinode/mcp.py:255` vs `palinode/mcp.py:203` for the sibling `palinode_list` tool, which uses the correct plurals.)
- **Recently shipped features unreachable from CLI:** `since_days` and `types` (#141/#157) live on the API and partially on MCP, but `palinode/cli/_api.py:13–22` forwards only `query, limit, category, context`. The user's primary entry point (`palinode search ...`) cannot use what was just shipped.
- **Defaults hidden in the wrong layer:** `cooldown_hours=24` and `threshold=0.75` for triggers come from server defaults that CLI/MCP callers can't see or override. MCP `palinode_trigger` exposes neither flag; CLI exposes only `threshold`; `_api.trigger_add` drops both `cooldown_hours` and `trigger_id` from its signature.
- **Duplicate JSON keys hiding values:** `palinode/mcp.py:624–625` has `"enum": [...]` declared twice on the prompt-task property. Python dict semantics make the second silently overwrite the first, deleting `nightly-consolidation` from MCP filters. Authored prompts with that task are invisible to MCP.
- **Parameter-name drift:** `file` (MCP rollback, MCP blame) vs `file_path` (everywhere else); `entities` (lists) vs `--entity` (repeated singulars) vs `project` (not at all). Every drift adds a wrong-name failure mode.
- **CLI bypassing the API:** `cli/read.py` reads disk directly; `cli/lint.py` falls back to a direct module import on connection failure; `cli/list.py` uses raw `httpx.get` instead of the shared `api_client`. Each bypass is a place where API fixes silently fail to propagate.
- **Source attribution leaking through plumbing:** `cli/save.py:50` defaults `source="cli"`, `mcp.py:727` defaults `"mcp"`, `server.py:700` defaults `"api"`, plugin passes through. The same memory written via two surfaces ends up with different `source:` frontmatter — analytics that bucket by source see drift that's plumbing, not workflow.
- **Several admin operations are CLI/API-only by design** (`reindex`, `rebuild-fts`, `split-layers`, `bootstrap-fact-ids`, `migrate-*`, `doctor`, `start`, `stop`, `config`). These are intentional; the ADR exempts them rather than aspiring to expose them through MCP.

The pattern is consistent: **API/MCP move first, CLI lags, the plugin is its own dialect, and the choke point is `_api.py`.** The fix is not heroics on each operation; it's a contract plus a forcing function.

## Decision detail

### `docs/PARITY.md` — the registry

A flat table per operation, listing the canonical name, canonical parameters, types, defaults, and per-surface notes. Reviewers and agents grep it before adding or renaming anything. Skeleton:

```
## save
| param      | type    | default | required | notes                          |
|------------|---------|---------|----------|--------------------------------|
| content    | string  | —       | yes      | markdown ok                    |
| type       | enum    | —       | yes¹     | enum lives in core/types.py    |
| ps         | bool    | false   | no       | shorthand for type=ProjectSnapshot |
| entities   | list    | []      | no       | refs like project/foo          |
| project    | string  | —       | no       | sugar; expanded to project/X   |
| source     | string  | header  | no       | propagated from X-Palinode-Source |
| ...        |         |         |          |                                |
¹ unless ps=true
```

A "Forbidden aliases" section lists known-bad spellings (e.g. `file` for `file_path`, `tags` for `entities`) so reviewers can grep them out.

### Exemption list (admin operations)

These are **not required** to appear on every surface. They appear on the surfaces where they make sense and the ADR commits us to leaving the others alone:

- `reindex`, `rebuild-fts`, `split-layers`, `bootstrap-fact-ids` — full database operations (CLI + API only)
- `migrate-openclaw`, `migrate-mem0` — one-off importers (CLI + API only)
- `doctor`, `start`, `stop`, `config`, `banner` — local/operational (CLI only)
- `health`, `health/watcher`, `git-stats`, `generate-summaries` — observability internals (API only)

If we ever want one of these on MCP/plugin, that's a separate ADR. The list is in `PARITY.md` and the parity test reads from it.

### `tests/test_surface_parity.py` — the forcing function

```
for op in PARITY_REGISTRY:
    if op.name in EXEMPT:
        continue
    cli_params  = collect_click_options(cli_registry, op.cli_command)
    mcp_params  = collect_mcp_properties(mcp_tools, op.mcp_tool)
    api_params  = collect_pydantic_fields(server.app, op.api_endpoint)
    plugin_params = collect_plugin_schema(op.plugin_tool) if op.plugin else None

    assert_param_set_equivalent(cli_params, mcp_params, api_params, plugin_params,
                                canonical=op.canonical_params)
```

Equivalence is name + shape (string vs array vs bool) + required-ness. Defaults are checked against a single `palinode/core/defaults.py` module rather than per-surface literals. Output-format differences are allowed by design (MCP returns text by default, API returns JSON, CLI is TTY-aware) and the test ignores them.

### Linter rule — HTTP-layer monopoly

A pre-commit grep that fails on `import httpx` or `httpx.` outside the two allowed files (`palinode/cli/_api.py`, `palinode/mcp.py`). All other CLI commands and any future plugin-side helpers must go through those two clients. This catches the four CLI files currently bypassing.

### Defaults module

`palinode/core/defaults.py`:

```
SEARCH_LIMIT_DEFAULT = config.search.default_limit
SEARCH_THRESHOLD_DEFAULT = config.search.api_threshold
TRIGGER_THRESHOLD_DEFAULT = 0.75
TRIGGER_COOLDOWN_HOURS_DEFAULT = 24
SAVE_SOURCE_HEADER = "X-Palinode-Source"
...
```

Every surface imports from there. No more `or 0.75` scattered across `_api.py`, `server.py`, and `cli/trigger.py`. The parity test asserts no surface has a literal default that contradicts this module.

## Consequences

### What this gives us

- **#159's class of bug fails CI** rather than getting filed by the next collaborator who tries `--project` on a sibling command.
- **The audit's ten findings become a closeable backlog** instead of a slow leak. Each is a PR that adds the missing param + updates `PARITY.md` + lets the test pass.
- **New features have an obvious surface checklist.** "Adding a flag" is now "edit PARITY.md, edit defaults.py, edit four files, run the test." That's tedious in the right way.
- **The plugin stops being a fifth dialect.** The plugin's TypeBox schemas are checked against the same registry (with a small JSON dump of the parity registry that the TS test imports).

### What we explicitly defer

- **Schema codegen** (one pydantic source, generated MCP/Click/TypeBox surfaces). The audit raised this as candidate #1, but committing to it now is overkill: the parity test gives us the safety we need without a refactor of every surface. If the test gets noisy enough that hand-syncing four surfaces becomes painful, that's the signal to revisit. Tracked as a follow-up, not a prerequisite.
- **Output-format unification.** CLI is TTY-aware (text/json), MCP is text-only with structured payloads embedded, API is JSON. This is by design (different consumers) and the parity test ignores output shape. If MCP needs structured returns later (#157 or downstream), that's its own ADR.
- **Plugin parity for everything.** The plugin today only exposes ~6 of the 17+ memory operations. The exemption list does not require us to fix this; the parity test only enforces parity for ops that *do* appear on the plugin. Expanding plugin coverage is a roadmap item, not a parity item.

### What this costs

- **One-time cost:** writing `PARITY.md` (a few hours' careful work — the audit already enumerated what belongs in it), writing the parity test (a day, including walking each surface's registry), fixing the ten findings (each is a small PR — total maybe a week of focused work).
- **Per-feature cost:** adding ~5–10 minutes per new flag for the four-surface dance. This is the cost we wanted — it makes adding a CLI-only flag friction the same as adding a plugin-only flag.
- **Migration risk:** renaming `file` → `file_path` on MCP `rollback` and `blame` is a breaking change for anyone who scripted against the current names. The migration is the parity-fix PR; surface a deprecation note for one release if needed.

### How this evolves

- **v0.5.x (now):** parity contract documented and tested; ten findings closed.
- **v0.6:** if the test starts catching real drift in the wild (i.e., we keep almost-merging unaligned PRs), promote candidate #1 — generate MCP/CLI/Plugin surfaces from pydantic. Until then, the contract + test is enough.
- **v1.0:** parity is a public-API guarantee. The registry doubles as the public schema documentation. New surfaces (e.g. a Cursor-native plugin, an HTTP/JSON-RPC bridge) are derived from the registry on day one.

## References

- ADR-001 (Tools Over Pipeline) — establishes the surfaces as the contract. This ADR is the implementation discipline ADR-001 implied but did not enforce.
- Issue #159 — the precipitator (`save --project`).
- 2026-04-26 cross-surface drift audit (full findings ranked by severity, retained in palinode memory under `project/palinode`).
- `CLAUDE.md` § "Code conventions" — the implicit parity rule this ADR makes explicit.
