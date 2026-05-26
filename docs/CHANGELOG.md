# Changelog

All notable changes to Palinode. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

## [0.8.12] — 2026-05-25

Targeted hot-path patch tail to 0.8.11. Headline fix: `auto_summary` is no longer called synchronously inside `/save`. The inline LLM call could block the response for the model's full first-token latency; on a cold or contended Ollama this presented to REST consumers as a "write timeout" while writes were succeeding server-side. The release also delivers `/health/auto-summary` and a `/status.auto_summary` block so monitor agents can detect a stalled async summary pipeline, bounded snippets on `/search-associative`, and a `recallProfile` config field with five named presets in the openclaw-palinode plugin.

### Changed

- **`auto_summary` is now async — moved off the `/save` hot path.** Previously `/save` called `_generate_summary` inline whenever a file qualified (`core:true`, no `summary`, content >= `min_content_chars`), blocking the response for the LLM's full first-token latency. On a cold or contended Ollama this could turn into a multi-second wait; REST consumers with conservative timeouts could see "write timeouts" while the write was actually succeeding server-side. The watcher already debounced `/generate-summaries` calls for files matching the same criteria (`palinode/indexer/watcher.py::_schedule_summary_generation`), so the fix is small: drop the inline call, surface `summary_pending: true` in the `/save` response when the file qualifies (mirroring the existing `description_pending` pattern), and let the watcher path do the LLM work out of band. Embeddings are unaffected — they still run inline.

### Added

- **`/health/auto-summary` endpoint.** New auth-exempt health check parallel to `/health/watcher`, designed for external monitor agents to detect a stalled async summary pipeline. Returns `status: "ok" | "degraded" | "down"` with a `reason` field. Probes the auto_summary Ollama URL (which may differ from the embed URL), scans for pending files (`core:true`, no summary, body >= threshold) capped at 1000, and applies a decision tree: `down` if Ollama unreachable; `degraded` if pending backlog ≥ 50 OR last run had errors with non-zero pending OR last run > 30min old with non-zero pending; `ok` otherwise. Disabled `auto_summary` always returns `ok` with a clarifying `reason`.
- **`auto_summary` block in `/status`.** Surfaces `last_run_at`, `last_run_duration_ms`, `last_run_count`, `last_run_errors`, `last_error`, `total_runs`, `total_errors` from a new module-level `_auto_summary_state` populated by `/generate-summaries` on every run. Mirrors the shape of the existing `reindex` and `write_time_*` observability blocks. Eleven regression tests in `tests/test_auto_summary_async.py`.

### Fixed

- **`/search-associative` now per-result snippet-enriches its response.** The associative spreading-activation endpoint was overlooked when `_enrich_with_snippets` landed for `/search` and `/search-recent`. A single associative hit on a multi-fact aggregated file could still return tens of KB of un-truncated `content`, defeating the budget guarantee that the rest of the search surface honours. Fix: one call to `_enrich_with_snippets(results, req.query, config.search.snippet_max_chars)` in `search_associative_api` mirrors the `/search` pattern. `content` is preserved untouched for API/CLI consumers; `snippet` and `content_truncated` are added per result. Three-test regression suite in `tests/test_search_associative_snippet.py` pins the behaviour.

### Plugin

- **openclaw-palinode: `recallProfile` config field + five named presets.** autoRecall was previously a binary switch with hardcoded four-source injection. A short prompt could expand into tens of thousands of tokens of effective input when semantic search matched large incident-style memory files and the model proceeded to reason about that prior incident rather than execute the current request. Size *and* modality were both wrong. Fix: named recall profiles (`coding`, `monitoring`, `investigation`, `writing`, `conversation`, plus `minimal` / `off`) compose source enablement, per-source caps, type allow/deny, and a total-budget hard cap. `coding` preserves existing behaviour (default if unspecified). `monitoring` ships triggers only, denies `RCA`/`Postmortem`/`Incident`/`Reflection`, caps at 3K. `recallProfileConfig` shallow-overrides individual fields without forking a preset. `autoRecall: false` still works (forces `off`). Plugin also switches semantic + associative result rendering to prefer the server's query-windowed `snippet` field over blunt `r.content.slice()`, with a fallback for older servers. Wrapping element now includes the profile name (`<palinode-memory profile="...">`) and the info log line surfaces profile + sources + final injection size for observability. Type filters (`type_allow` / `type_deny`) are forwarded defensively to `/search` and `/search-associative`; older Palinode servers ignore unknown body keys. Seven unit tests in `plugin/test/recall-profile.test.ts` pin the catalog shape and override semantics.

## [0.8.11] — 2026-05-25

M1 hardening tail + audit-driven critical-bug remediation. 13 issues closed across leak-regression guards (#341, #275-prep), MCP registry hygiene (#313), doctor FTS5 sync surfacing (#316), cross-surface session-end timeout (#377), /wrap push semantics (#353), embedder logging (#383), four silent-failure-returns-success criticals (#384–#387), Ollama context-window hardening (#335), and graceful-degrade auto-description (#336). Two audit deliverables shipped on dedicated branches (#337 logging, #378 session-end). One investigation comment drafted (#373 Claude Desktop). #338 (centralized Ollama client) explicitly deferred to v0.8.12.

### Changed

- **`upsert_chunks` return contract: `int` → `dict{written, vec_ok, fts_ok}` (#385).** The function previously returned a bare integer (chunks written). It now returns a dict so callers can detect per-index health without a separate query. `index_file` and `POST /save` both surface `indexed_vec` and `indexed_fts` from this result. MCP `palinode_save` warns when either flag is False.

### Fixed

- **`upsert_chunks` vec0 and FTS5 write failures are now logged and surfaced (#385 / C3).** Both the pre-INSERT DELETE and the INSERT-after-DELETE pair on `chunks_vec` were wrapped in bare `except Exception: pass`. A vec0 structural failure silently produced a `chunks` row with no corresponding `chunks_vec` row — the chunk was FTS5-searchable but invisible to vector search. Fix: vec0 final-retry failures log at ERROR with `exc_info=True`; FTS5 sync failures log at WARNING; the INSERT-first FTS5 pattern avoids the "malformed" DELETE-on-empty bug.
- **`POST /save` now returns `git_committed`, `indexed_vec`, and `indexed_fts` (#386 / C4, #385).** The git auto-commit block logged at error level but the response had no field — `git_committed: false` was invisible to callers. Now `git_committed` is True only when `auto_commit=True` and the git subprocess completes without raising. Added `exc_info=True` to the error log. `indexed_vec` and `indexed_fts` are surfaced from `index_file` outcome. MCP `palinode_save` uses all three flags for the `_save_warnings` path.
- **Consolidation runner YAML parse failures now log and count instead of silently skipping (#387 / C5).** `_collect_daily_notes` and `_get_decisions_for_project` both had `except Exception: pass/continue` on YAML parse errors. Corrupt frontmatter silently dropped files from consolidation. Fix: both log WARNING with filepath, exception, and recovery hint ("run `palinode lint`"). `_collect_daily_notes` returns `(notes, skipped_count)`; callers include `yaml_parse_errors` in the run summary when non-zero. Body text is still collected even when frontmatter fails.
- **`recent_save_embeddings` DB open failure now logs WARNING instead of silently returning `[]` (#384 / C2).** A missing or corrupt DB caused the dedup window to silently disable, making every save appear unique. Now logs at WARNING with `db_path` and exception so operators can correlate dedup misses with infrastructure issues.

- **`palinode/core/embedder.py` now has a logger; all Ollama failure paths surface (#383 / C1).** The module had no logger at all — every embed failure (timeout, HTTP error, connect error, unexpected response shape) was silent, forcing operators to correlate downstream symptoms (zero search hits, dedup misses, Phase 7 smoke flakes) with no log context. Added `logger = logging.getLogger(__name__)` and per-failure log calls: each exception class (TimeoutException, HTTPStatusError, ConnectError) logs at WARNING with `exc_info=True` and structured context (model, endpoint, text_len, timeout setting, retry index). Unexpected-shape 200 responses log at WARNING with `response_keys`. Successful embed logs at DEBUG with timing. Terminal all-endpoints-exhausted case logs at WARNING with model, url, text_len, and timeout_seconds — the exact context needed to correlate with smoke-rig reports. Raw text is never logged; only `text_len` appears so PII does not reach log aggregators.
- **Session-end timeout raised to 90s on all three surfaces (#377).** The 30s CLI timeout and 10s hook curl timeout were too short for multi-decision payloads (BGE-M3 dedup embed + git commit on a remote Tailscale host). All three surfaces now use a shared `SESSION_END_TIMEOUT_SECONDS = 90.0` constant from `palinode.core.defaults` (overridable via `PALINODE_HOOK_TIMEOUT` / `PALINODE_SESSION_END_TIMEOUT` env vars). CLI, MCP, and hook all import or reference the same source of truth; module-load assertion guards in `cli/_api.py` and `mcp.py` catch future drift at import time. Hook script `PALINODE_HOOK_TIMEOUT` default is 30s; `settings.json` runner timeout raised to 35s to stay ahead of curl. CLI now distinguishes `ReadTimeout` (slow API, recoverable) from `ConnectError` (API down) with separate error messages. Eight new regression tests in `tests/test_session_end_timeout.py`.
- **`/wrap` now pushes to remote before archiving (#353).** The `/wrap` slash command calls `palinode_push` (MCP tool) before `palinode_session_end`, so local commits are on the remote before the daily note is written and context is cleared. Skips silently when there are no unpushed commits or no remote is configured; surfaces a clear message on push failure so Paul can decide whether to proceed. `palinode init`'s `WRAP_COMMAND_BODY` is updated to match.
- **Embed path hardened against Ollama context-window mismatch (#335).** When Ollama returns a 200 OK with `{"error": "prompt is too long for max context"}` (its silent-truncation failure mode when `num_ctx` is too small), `_embed_local` now raises `EmbeddingContextError` — a typed exception with `model`, `text_len`, and `ollama_message` attributes and a recovery hint pointing at the modelfile fix. Added a lazy preflight check (`check_model_context`) that queries `/api/show` on the first embed call and logs a WARNING if `num_ctx` is below 8192 (bge-m3's actual capability vs. Ollama's 4096 default — the exact regression from 2026-05-04). Preflight runs once per process (thread-safe) and never blocks embed on failure.
- **`palinode_save` auto-description now has a hard timeout with graceful degrade (#336).** Previously the description-generation call to Ollama used a hardcoded 15 s timeout with no degrade path — a cold-loading or overloaded Ollama would stall every `/save` for the full duration. Now: (1) `_generate_description` has a configurable timeout (`config.auto_summary.describe_timeout_seconds`, default 5 s, override via `PALINODE_DESCRIBE_TIMEOUT_SECONDS`) that returns a `_DESCRIPTION_DEFERRED` sentinel on `TimeoutException`; (2) the `/save` handler detects the sentinel, saves the file without a description, and includes `description_pending: true` in the response so callers know to expect a fill-in; (3) the watcher detects files with no `description` field after any file event and schedules a debounced retry via `/generate-summaries` (10 s window, accumulates rapid saves). The audit Q2 finding is also fixed: non-timeout description failures (e.g. `ConnectError`) were logged at `INFO`; they now log at `WARNING`.

### Added

- `tests/test_server_json_version_alignment.py` — CI guard that fails if `server.json` top-level `version` or any `packages[*].version` drifts from `pyproject.toml[project.version]`. Silent drift was possible: `mcp-publisher publish` would ship the manifest with the wrong version pin with no runtime error to surface it. Four tests cover: top-level version match, per-package version match, JSON parseability, and TOML parseability — fail loud on missing files or corrupt parse so no check silently passes (#313).
- **`palinode doctor` FTS5 sync check (#316).** New `fts5_sync` check detects row-count drift between the `chunks` source table and the `chunks_fts_docsize` FTS5 shadow table. A mid-write exception, schema migration, or crashed bulk-index run can leave FTS5 with fewer entries than `chunks`, causing BM25 keyword search to silently miss content. The check compares the two counts and fails with WARN + recovery hint ("run `palinode reindex`") when they diverge. Tagged `fast` — pure SQLite read, no network I/O. Surfaces automatically through `palinode_doctor` MCP tool and `GET /doctor?fast=true` API endpoint.

## [0.8.10] — 2026-05-22

M1 hardening + UX polish. This release carries forward the newer hardening, CLI, and docs-discipline work after the earlier public `0.8.9` reliability release. No breaking changes; one new opt-in MCP parameter (`palinode_search full=True`).

### Removed

- **Stop tracking `build/` and `dist/` artifacts in git (#295).** 45 stale build outputs (43 from `build/lib/palinode/*` and 2 from `dist/palinode-0.7.0*` — three releases old) were checked in. They go stale fast, bloat clones, and confuse `pip install -e .`. They were also the largest leak category caught by the 2026-05-01 pre-v0.8.7 scrub audit that triggered the #292 belt-and-suspenders path scrub. `.gitignore` now excludes `build/`, `dist/`, and `*.egg-info/` generically; regression guard in `tests/test_no_build_artifacts_tracked.py` fails CI if anyone re-adds them.

### Changed

- **`/search` API now populates a bounded `snippet` field on every result, and `palinode_search` MCP tool renders it by default (#352).** Pathologically large chunks (flat files with no section breaks could produce 50KB+ single chunks) used to blow the MCP tool-result budget — two routine searches were enough to exceed Claude Code's ceiling. The fix splits the contract: API preserves full `content` for CLI/API consumers, adds `snippet` (default cap 400 chars, configurable via `config.search.snippet_max_chars`) windowed on the first matched query term. MCP renders snippet by default with a "use palinode_read for full text" footer when anything was truncated; new `full=True` parameter on `palinode_search` opts into a 4KB-capped content render for the rare case it's needed. `palinode search` CLI also prefers the new `snippet` field over its prior blind 200-char content truncation, so TTY output now shows a centered match window instead of arbitrary leading text — with a legacy fallback when talking to an older API server.
- **Silent-misconfiguration paths now warn loud-and-recoverably (M1 hardening, #273 + #354).**
  - `load_config()` emits a `WARNING` when no `palinode.config.yaml` is found and built-in defaults are loaded, listing every path searched plus the recovery hint ("set `PALINODE_DIR` or drop a config file at..."). The stderr startup banner also changes from `Palinode config: defaults` to `Palinode config: ⚠ defaults (no config file found)` so production deployments where systemd wires `PALINODE_DIR` but an interactive ssh session doesn't can't silently run `palinode lint` against the wrong filesystem (#273).
  - API server lifespan warns at startup when `config.git.auto_commit` is enabled but `memory_dir` is not a git repository — every `/save`'s `git commit` was silently no-op'ing with no operator signal. The warning includes the exact `git init <dir>` recovery command (#354).

### Documentation

- **`/save` endpoint docstring now shows the canonical request schema (#299)** — FastAPI's auto-generated `/docs` UI renders the docstring, so new integrators see `{content, type, slug, entities, title}` directly. Explicitly notes that the body field is `content` (not `body`) and that `category` is *derived* from `type`, not a separate input — the legacy/wrong shape that misled at least one smoke-test prompt.
- **README API reference and inline `/save` endpoint docstring now document the request-body size cap (#298)** — `PALINODE_MAX_REQUEST_BYTES` default 5 MB. Code, README, OPERATIONS.md, and the endpoint docstring are now aligned. Added `tests/test_docs_schema_alignment.py` (5 tests) to keep code and docs in sync — fails CI if the constant changes without the docs being updated, or if the SaveRequest model grows a `body`/`category` field.

### Fixed

- **Executor writes are now crash-safe (#310).** The deterministic consolidation executor now writes both target markdown files and `-history.md` sidecars via temp-file + `fsync` + atomic replace, so a mid-write crash preserves the last complete on-disk state instead of truncating a memory file.
- **`palinode config view` no longer crashes with `TypeError: isinstance() arg 2 must be a type` (#274).** Root cause: `from palinode.cli.list import list_cmd` had the side effect of binding the `palinode.cli.list` submodule onto the `palinode.cli` package namespace, shadowing the builtin `list`. The nested `to_dict` helper in `config_view` then resolved `list` to the submodule and `isinstance(obj, <module>)` raised. Fixed by renaming the submodule to `palinode/cli/list_cmd.py` (the CLI command name `palinode list` is unchanged). Also added a `builtins.list` reference + defensive `try/except TypeError` fallback in `to_dict` so any future unserialisable field degrades to `repr()` instead of crashing the whole view. While in the area, also fixed an undefined-`console` reference in `config_view` (separate latent bug previously hidden by the TypeError).

### Added

- Release-blocking `mcp-tool-coverage` CI job in `.github/workflows/ci.yml` and `.github/workflows/main-ci.yml` — runs `test_mcp_e2e.py` + `test_mcp_stdio.py` without `continue-on-error`, gating every PR and post-merge sweep on full MCP tool coverage (#346, parent #342).
- `docs/LAUNCH-CHECKLIST.md` "Harness smoke (release blocker)" section with 9 per-harness checkboxes (Tier 1: Claude Code, Codex, Antigravity; Tier 2: Cursor, Claude Desktop, Cline, Zed, Windsurf, Continue) and a pointer to `docs/HARNESS-SMOKE.md` for procedures (#346, parent #342).
- PR template checkbox reminding contributors to update `TOOL_SMOKE_ARGS` when adding or removing MCP tools (#346, parent #342).
- `tests/test_launch_checklist_harness.py` and `tests/test_pr_template_smoke_args.py` regression guards for the new CI-gate infrastructure (#346, parent #342).
- `docs/HARNESS-SMOKE.md` — per-harness smoke checklist covering all 9 Tier 1+2 MCP harnesses (Claude Code, Codex, Antigravity, Cursor, Claude Desktop, Cline, Zed, Windsurf, Continue) with a canonical 5-call sequence, expected output snippets, and troubleshooting notes. Tier 3 harnesses (OpenClaw, Hermes AI, Pi) documented as future/best-effort (#345, parent #342).
- `palinode mcp-smoke` CLI subcommand — `--list` prints all supported harnesses with tier info (TTY-aware), `<harness>` prints a copy-paste smoke runbook, `--json` emits a parseable record, and `--record` appends a JSONL entry to `.palinode/harness-smoke-runs.jsonl` for launch-gate validation in Phase 4 (#346). Tier 3 harnesses are hard-refused with an explanatory message (#345, parent #342).
- `docs/MCP-CONFIG-HOMES.md` — added Codex CLI (`~/.codex/config.toml`, TOML format) and Antigravity (`~/.gemini/antigravity/mcp_config.json`) config-path sections (#345).
- `tests/integration/test_mcp_stdio.py` — end-to-end stdio JSON-RPC test that spawns `palinode-api` on a random port and drives `palinode-mcp` via real MCP `stdio_client` + `ClientSession` (#344, parent #342). Verifies the transport layer that every harness uses: `initialize` handshake, `tools/list` completeness, and `tools/call` over stdio for all 25 tools. Catches stdio framing, JSON-RPC ID handling, lifecycle, and env-var inheritance bugs that the in-process Phase 1 test (#343) cannot. Marked `@pytest.mark.slow` (~23s due to subprocess startup).
- `tests/integration/_smoke_args.py` — shared `TOOL_SMOKE_ARGS` registry consumed by both `test_mcp_e2e.py` (Phase 1) and `test_mcp_stdio.py` (Phase 2). Single source of truth keeps in-process and stdio coverage in lockstep; the drift guard in `test_mcp_e2e.py` covers both.

### Plugin

- **OpenClaw plugin updated for 2026.5.x compatibility (#368).** Type and dev-dependency alignment for the OpenClaw plugin tree (`@types/node`, `typescript`) with the latest OpenClaw release; `package-lock.json` regenerated. No product-surface changes.

## [0.8.9] — 2026-05-14

M1 hardening + UX polish. Six issues closed across the silent-misconfiguration cluster (config defaults, git persistence), CLI bug fixes, MCP tool-result budget overruns, and per-PR CHANGELOG/doc discipline. No breaking changes; one new opt-in MCP parameter (`palinode_search full=True`).

### Removed

- **Stop tracking `build/` and `dist/` artifacts in git (#295).** 45 stale build outputs (43 from `build/lib/palinode/*` and 2 from `dist/palinode-0.7.0*` — three releases old) were checked in. They go stale fast, bloat clones, and confuse `pip install -e .`. They were also the largest leak category caught by the 2026-05-01 pre-v0.8.7 scrub audit that triggered the #292 belt-and-suspenders path scrub. `.gitignore` now excludes `build/`, `dist/`, and `*.egg-info/` generically; regression guard in `tests/test_no_build_artifacts_tracked.py` fails CI if anyone re-adds them.

### Changed

- **`/search` API now populates a bounded `snippet` field on every result, and `palinode_search` MCP tool renders it by default (#352).** Pathologically large chunks (flat files with no section breaks could produce 50KB+ single chunks) used to blow the MCP tool-result budget — two routine searches were enough to exceed Claude Code's ceiling. The fix splits the contract: API preserves full `content` for CLI/API consumers, adds `snippet` (default cap 400 chars, configurable via `config.search.snippet_max_chars`) windowed on the first matched query term. MCP renders snippet by default with a "use palinode_read for full text" footer when anything was truncated; new `full=True` parameter on `palinode_search` opts into a 4KB-capped content render for the rare case it's needed. `palinode search` CLI also prefers the new `snippet` field over its prior blind 200-char content truncation, so TTY output now shows a centered match window instead of arbitrary leading text — with a legacy fallback when talking to an older API server.
- **Silent-misconfiguration paths now warn loud-and-recoverably (M1 hardening, #273 + #354).**
  - `load_config()` emits a `WARNING` when no `palinode.config.yaml` is found and built-in defaults are loaded, listing every path searched plus the recovery hint ("set `PALINODE_DIR` or drop a config file at..."). The stderr startup banner also changes from `Palinode config: defaults` to `Palinode config: ⚠ defaults (no config file found)` so production deployments where systemd wires `PALINODE_DIR` but an interactive ssh session doesn't can't silently run `palinode lint` against the wrong filesystem (#273).
  - API server lifespan warns at startup when `config.git.auto_commit` is enabled but `memory_dir` is not a git repository — every `/save`'s `git commit` was silently no-op'ing with no operator signal. The warning includes the exact `git init <dir>` recovery command (#354).

### Documentation

- **`/save` endpoint docstring now shows the canonical request schema (#299)** — FastAPI's auto-generated `/docs` UI renders the docstring, so new integrators see `{content, type, slug, entities, title}` directly. Explicitly notes that the body field is `content` (not `body`) and that `category` is *derived* from `type`, not a separate input — the legacy/wrong shape that misled at least one smoke-test prompt.
- **README API reference and inline `/save` endpoint docstring now document the request-body size cap (#298)** — `PALINODE_MAX_REQUEST_BYTES` default 5 MB. Code, README, OPERATIONS.md, and the endpoint docstring are now aligned. Added `tests/test_docs_schema_alignment.py` (5 tests) to keep code and docs in sync — fails CI if the constant changes without the docs being updated, or if the SaveRequest model grows a `body`/`category` field.

### Fixed

- **`palinode config view` no longer crashes with `TypeError: isinstance() arg 2 must be a type` (#274).** Root cause: `from palinode.cli.list import list_cmd` had the side effect of binding the `palinode.cli.list` submodule onto the `palinode.cli` package namespace, shadowing the builtin `list`. The nested `to_dict` helper in `config_view` then resolved `list` to the submodule and `isinstance(obj, <module>)` raised. Fixed by renaming the submodule to `palinode/cli/list_cmd.py` (the CLI command name `palinode list` is unchanged). Also added a `builtins.list` reference + defensive `try/except TypeError` fallback in `to_dict` so any future unserialisable field degrades to `repr()` instead of crashing the whole view. While in the area, also fixed an undefined-`console` reference in `config_view` (separate latent bug previously hidden by the TypeError).

### Added

- Release-blocking `mcp-tool-coverage` CI job in `.github/workflows/ci.yml` and `.github/workflows/main-ci.yml` — runs `test_mcp_e2e.py` + `test_mcp_stdio.py` without `continue-on-error`, gating every PR and post-merge sweep on full MCP tool coverage (#346, parent #342).
- `docs/LAUNCH-CHECKLIST.md` "Harness smoke (release blocker)" section with 9 per-harness checkboxes (Tier 1: Claude Code, Codex, Antigravity; Tier 2: Cursor, Claude Desktop, Cline, Zed, Windsurf, Continue) and a pointer to `docs/HARNESS-SMOKE.md` for procedures (#346, parent #342).
- PR template checkbox reminding contributors to update `TOOL_SMOKE_ARGS` when adding or removing MCP tools (#346, parent #342).
- `tests/test_launch_checklist_harness.py` and `tests/test_pr_template_smoke_args.py` regression guards for the new CI-gate infrastructure (#346, parent #342).
- `docs/HARNESS-SMOKE.md` — per-harness smoke checklist covering all 9 Tier 1+2 MCP harnesses (Claude Code, Codex, Antigravity, Cursor, Claude Desktop, Cline, Zed, Windsurf, Continue) with a canonical 5-call sequence, expected output snippets, and troubleshooting notes. Tier 3 harnesses (OpenClaw, Hermes AI, Pi) documented as future/best-effort (#345, parent #342).
- `palinode mcp-smoke` CLI subcommand — `--list` prints all supported harnesses with tier info (TTY-aware), `<harness>` prints a copy-paste smoke runbook, `--json` emits a parseable record, and `--record` appends a JSONL entry to `.palinode/harness-smoke-runs.jsonl` for launch-gate validation in Phase 4 (#346). Tier 3 harnesses are hard-refused with an explanatory message (#345, parent #342).
- `docs/MCP-CONFIG-HOMES.md` — added Codex CLI (`~/.codex/config.toml`, TOML format) and Antigravity (`~/.gemini/antigravity/mcp_config.json`) config-path sections (#345).
- `tests/integration/test_mcp_stdio.py` — end-to-end stdio JSON-RPC test that spawns `palinode-api` on a random port and drives `palinode-mcp` via real MCP `stdio_client` + `ClientSession` (#344, parent #342). Verifies the transport layer that every harness uses: `initialize` handshake, `tools/list` completeness, and `tools/call` over stdio for all 25 tools. Catches stdio framing, JSON-RPC ID handling, lifecycle, and env-var inheritance bugs that the in-process Phase 1 test (#343) cannot. Marked `@pytest.mark.slow` (~23s due to subprocess startup).
- `tests/integration/_smoke_args.py` — shared `TOOL_SMOKE_ARGS` registry consumed by both `test_mcp_e2e.py` (Phase 1) and `test_mcp_stdio.py` (Phase 2). Single source of truth keeps in-process and stdio coverage in lockstep; the drift guard in `test_mcp_e2e.py` covers both.

## [0.8.8] — 2026-05-01

A pure-security release in response to the MCP Marketplace security audit.
Closes 27 of the 30 findings (5 critical, 8 high, 12 medium, 2 low). No
breaking changes; all hardening is additive. Local-first deployments keep
working without ceremony.

### Security

- **Optional bearer-token authentication on the API server.** Set
  `PALINODE_API_TOKEN` (or `PALINODE_API_TOKEN_FILE` for docker-secrets
  style deployments) to require `Authorization: Bearer <token>` on every
  request other than `/health` and `/health/watcher`. Off by default to
  preserve zero-friction local dev. The startup gate now refuses to launch
  when `PALINODE_API_BIND_INTENT=public` is set without a token, closing
  the public-bind hardening gap. Comparison uses `hmac.compare_digest` to
  remove the timing side-channel. The gate fires at module import so it
  triggers under `uvicorn palinode.api.server:app` (the canonical systemd
  ExecStart pattern), not just from `main()`.
- **Dependency lower-bounds added** to exclude 15 known CVEs against
  `pyyaml`, `httpx`, `sqlite-vec`, `fastapi`, `uvicorn`, `pydantic`, and
  `mcp`. All currently-installed versions are already past the fix
  windows; this codifies that floor.
- **Path traversal hardening** in `_resolve_memory_path` migrated from
  `os.path.realpath`/`commonpath` to `pathlib.Path.resolve(strict=True)`
  + `Path.is_relative_to()`. Generic `"Invalid path"` returned to clients
  (no filesystem-info leak); original input logged at INFO for operators.
- **Streaming body-size enforcement.** New ASGI middleware counts bytes
  per chunk and rejects with HTTP 413 mid-stream. Closes the
  chunked-encoding bypass that the previous header-only check missed.
- **Wiki footer entity validation.** Slugs must match `^[A-Za-z0-9._-]+$`
  before being emitted inside `[[wikilinks]]`; malformed slugs are
  dropped with a warning rather than corrupting markdown.
- **LLM prompt content** in `_generate_description` and `_generate_summary`
  now wrapped in `<user_content>` delimited tags with neutralization of
  injected close tags. Best-effort defense against prompt-injection.
- **Subprocess argv-form audit** confirmed all `subprocess.run` calls in
  the codebase use list-of-args, never `shell=True`. AST-based test in
  `tests/test_subprocess_argv_form.py` fails CI on any future regression.
- **CORS wildcard rejection** at startup. `PALINODE_CORS_ORIGINS=*` now
  refuses to launch with a clear error; each origin must parse as a
  valid http/https URL with non-empty netloc.
- **Secret-redacting logging filter** prevents API keys (`sk-...`,
  `xoxb-...`, AWS keys) and basic-auth-in-URL credentials from leaking
  via `logger.exception()` tracebacks if a memory file contains them.
- **Rate limiter dict bounded.** `_rate_counters` now prunes expired
  entries on every check and caps at `PALINODE_RATE_LIMIT_MAX_KEYS`
  (default 10000) with oldest-window eviction. Prevents memory growth
  under varied-IP scans.
- **TOCTOU mitigation** in file reads. New `_open_memory_file_text()`
  helper uses `os.open(..., O_RDONLY | O_NOFOLLOW)` so symlink swaps
  raise `OSError` rather than silently following the link.
- **Narrowed broad `except Exception` blocks** at LLM, HTTP, and git
  call sites. Specific exception types (`httpx.HTTPError`, `OSError`,
  `json.JSONDecodeError`, etc.) make root-cause tracing easier and
  prevent operational errors from masking security signal.

### Fixed

- CI security-scan job: bandit findings annotated with `# nosec` plus
  rationale rather than suppressed wholesale. `pip-audit` now runs (was
  blocked by the bandit failure in v0.8.5).
- `INSTALL-CLAUDE-CODE.md` tool count corrected (21 → 25) and four
  previously-undocumented tools added to the table:
  `palinode_cluster_neighbors`, `palinode_topic_coverage`,
  `palinode_depends`, and `palinode_timeline` (deprecated alias).

### Internal

- Confirmed `search_api` uses parameterized SQL throughout, FTS5 input
  sanitization, and bounded result-limits — no LLM call inside.
- Pre-existing test flake `test_session_end_creates_daily_and_individual`
  (dedup collision) is unrelated to this release; tracked separately.

## [0.8.6] — 2026-04-29

### Added

- `pyproject.toml` bumped to **v0.8.6** and `[tool.setuptools] packages` list corrected: `palinode.diagnostics`, `palinode.diagnostics.checks`, `palinode.import_`, and `palinode.lint` were missing and would have been silently omitted from the wheel. All declared packages now match on-disk layout.
- `palinode mcp-config --diagnose` now covers **Roo Cline** (`rooveterinaryinc.roo-cline`) in addition to the original Cline extension. Roo Cline uses a different extension ID and settings filename (`mcp_settings.json` instead of `cline_mcp_settings.json`).
- `docs/MCP-INSTALL-RECIPES.md` — new **JetBrains AI Assistant** section (section 6): stdio and HTTP snippets, settings UI path, Settings Sync note, troubleshooting table. Covers IntelliJ IDEA, PyCharm, WebStorm, GoLand, Rider, CLion, DataGrip, RubyMine.
- `docs/MCP-INSTALL-RECIPES.md` — transport quick reference expanded into a "which transport?" decision block with use-when guidance.
- `docs/MCP-CONFIG-HOMES.md` — JetBrains section added (UI-first, version-specific path caveat) and Roo Cline paths added. Closes public #24.
- `tests/integration/test_mcp_e2e.py` — E2E test suite for the MCP client flow: exercises every major MCP tool (search, save, session_end, status, read, history, doctor, list) via in-process FastAPI dispatch with no Ollama required (#122).
- `tests/integration/test_security.py` — Security test suite covering OWASP top-10: path traversal, null bytes, symlink escape, SQL injection, SSRF, CORS enforcement, rate limiting, request size limit, no stack traces, YAML injection, CRLF header injection, and XSS/script injection (#123).
- `palinode_cluster_neighbors` / `palinode cluster-neighbors` / `POST /cluster-neighbors` — given a memory file path, returns the top-K semantically related files that are NOT already wikilinked to or from it; surfaces implicit relationships for the LLM to propose new cross-links (#235).
- `palinode_topic_coverage` / `palinode topic-coverage` / `POST /topic-coverage` — given a topic phrase, returns `{covered, best_match, similarity}` indicating whether an existing wiki page already covers the topic; use before ingesting new content to avoid redundancy (#235).
- Both new tools are exposed across all four cross-surface targets (MCP, REST API, CLI, parity registry) and covered by `tests/test_embedding_tools.py` (#235).
- IETF KU frontmatter alignment (issue #106): `parse_ku_fields()` in `palinode/core/parser.py` recognizes `ku_version`, `confidence`, and `lifecycle` fields; `config.ku_compat` flag (default `enabled: false`) controls auto-population on save; `confidence` is surfaced as a top-level key in search results when set. See `docs/HOW-MEMORY-WORKS.md` for field semantics.
- `palinode import --from-vault <path>` — import existing Obsidian vault .md files into the palinode memory store. Infers category from PARA directory structure (Projects→`projects/`, Areas→`decisions/`, Resources→`research/`, Archive→`archive/`), daily-note filename patterns, and frontmatter `type:` field. Rewrites wikilinks to point at new slugged paths; orphaned links are left as-is with a warning. Supports `--apply` (default is dry-run), `--overwrite`, and `--into-category` override. Implemented in `palinode/import_/vault.py` and `palinode/cli/import_vault.py` (#236).
- `flake.nix`, `nix/services/palinode-service.nix`, `nix/services/mcp-service.nix` — Nix flake and NixOS service modules for palinode API, watcher, and MCP server; community contribution welcome for refinement on real NixOS boxes (#38).
- `external_refs` frontmatter field for SDLC object linking — attach GitLab MR/issue/pipeline, GitHub PR, Linear, Jira, or any free-form key/value ref to a memory at save time. Supported across API (`POST /save`), CLI (`palinode save --external-ref KEY=VALUE`, repeatable), MCP (`palinode_save`), and plugin. Recognised keys render with pretty labels in search results; unrecognised keys pass through unchanged (#115).
- `palinode_depends` — milestone dependency modeling tool (#97). Reads `depends_on`, `blocks`, and `parallel_with` frontmatter from ProjectSnapshot files and returns the dependency neighbourhood for a slug; `--unblocked` (CLI) / `unblocked=true` (MCP/REST) returns all items whose every dependency is done. Exposed on all four surfaces: MCP (`palinode_depends`), REST (`GET /depends/{slug}`, `GET /depends/_unblocked`), CLI (`palinode depends <slug>`), and plugin (`palinode_depends`).
- `palinode lint --deep-contradictions` — opt-in LLM-confirmed semantic contradiction check across all `type: Decision` memories. Uses embedding similarity to identify candidate pairs (configurable `--similarity-threshold`, default 0.75), then calls the configured LLM endpoint to classify each as CONTRADICTION / AGREEMENT / UNRELATED. Hard cap via `--max-llm-calls` (default 50). Default `palinode lint` is unchanged and makes no LLM calls (#98).
- `tests/test_mcp_tool_count.py` — assertion test that keeps the `docs/MCP-SETUP.md` available-tools table in sync with `palinode/mcp.py` registered tools (#238).
- `tests/integration/test_mcp_e2e.py` — every-tool smoke registry (`TOOL_SMOKE_ARGS`) and parametrized `test_every_tool_dispatches` exercise all 25 MCP tools in-process with minimal valid args; a drift guard `test_smoke_args_covers_all_tools` sources the tool list from `@server.list_tools()` so a new tool added without a smoke entry fails with a clear instruction (#343, parent #342). Lenient flag tolerates graceful errors for tools that legitimately can't complete in a sealed test env (`palinode_ingest`, `palinode_consolidate`, `palinode_push`, `palinode_doctor_deep`).
- `docs/LAUNCH-CHECKLIST.md` — pre-launch readiness checklist for v1.0 (#125).
- `docs/MCP-SETUP.md` — Codex CLI section (#52).
- `docs/INSTALL-CLAUDE-CODE.md` — Cursor, Antigravity IDE, and Codex CLI sections with skill paths and MCP config locations (#52).
- `README.md` — "Supported Platforms" table listing all supported clients (#52).
- GitHub Actions CI workflow (`.github/workflows/ci.yml`) with unit-tests (Python 3.11/3.12 matrix), integration-tests, and security-scan jobs triggered on every push and PR (#121).
- GitHub Actions post-merge sweep (`.github/workflows/main-ci.yml`) triggered on push to `main`; auto-opens a GitHub issue on regression (#198).
- User-facing slash commands (`/wrap`, `/save`, `/ps`) audited and documented as deterministic — each command maps to a single named tool with a fixed argument shape; LLM synthesizes content, not routing (issue #138).
- Integration test suite expanded to 24 tests in `tests/integration/test_api_roundtrip.py`, covering git-commit behaviour, CORS origin enforcement, additional path-traversal and null-byte cases, missing-field validation, and an explicit save-index-search-read roundtrip. (issue #120)
- Retrieval-event instrumentation (#256): every `palinode_search`, `palinode_read`, `palinode_history`, and `palinode_blame` call now appends a structured `RetrievalEvent` to `.audit/retrievals.jsonl`, distinguishing `explicit` (tool-call) from `passive` (auto-inject) modes. No ranker behavior change.
- `palinode retrieval-stats` CLI command reads the JSONL log and reports event totals, explicit/passive breakdown, top-20 retrieved files, retrieval-frequency distribution, and mean/median time-since-last-retrieval.
- `instrumentation.capture_retrievals` config key (default `true`) and `PALINODE_INSTRUMENTATION_DISABLED=1` env var to suppress retrieval logging for privacy.

### Changed

- `PalinodeAPI.__init__` now accepts an optional `client: httpx.Client` argument for test injection (#197).
- `palinode_history` now accepts `detail="full"` for commit-level diffs (#32); `palinode_timeline` added as a deprecated alias.
- Plugin TypeScript schemas now match all canonical params (#176) — 11 missing params added to `palinode_search` and `palinode_save`; cross-surface parity contract is fully enforced for the plugin surface.
- `docs/MCP-SETUP.md` — removed prose tool count; the available-tools table is now the source of truth; added `palinode_doctor` and `palinode_doctor_deep` rows (#238).
- `docs/MCP-SETUP.md`, `docs/MCP-INSTALL-RECIPES.md`, `deploy/systemd/README.md`, `README.md` — clarified that `palinode-mcp-sse` serves streamable-HTTP at `/mcp/` (name is historical); use `"type": "http"` and always include the trailing slash (#258).
- Nightly consolidation (`nightly.allowed_ops`) now includes `MERGE` (#202). The executor enforces a same-day guard: only facts sharing the same `[YYYY-MM-DD]` date prefix may be merged in a nightly run. Cross-date or undated MERGE proposals are rejected with a log warning and counted as `merge_rejected` in the stats dict.

### Fixed

- Default `audit.log_path` is now resolved to an absolute path under `memory_dir` at config load time, eliminating the spurious `audit_log_writable` doctor warning on every fresh install (#254).
- `mcp_config_homes` doctor check no longer reports a misleading "run \`palinode init\`" message when palinode is running over SSH stdio — detects `SSH_CONNECTION` and returns an informational result explaining the remote context (#255).
- `0.0.0.0` binding warning is now suppressed when `PALINODE_API_BIND_INTENT=public` is set, allowing intentional network-exposed deployments (e.g., Tailscale) to start quietly. The systemd API service template sets this by default (#253).
- `POST /save` now writes `last_updated` equal to `created_at` on initial file creation so freshly saved memories are not immediately flagged as stale by the freshness checker (#177).
- `check_freshness()` now computes per-section hashes (matching the indexer) instead of a whole-body hash; multi-section files no longer always report stale (#203).
- `palinode init` HOOK_SCRIPT (the scaffolded SessionEnd hook) now uses `jq -s` slurp extraction for both MSG_COUNT and FIRST_PROMPT, eliminating the SIGPIPE class entirely. #257 fixed the same bug pattern in `examples/hooks/palinode-session-end.sh` but the scaffolded version in `palinode/cli/init.py` was missed; #267 closes that gap so `palinode init` produces a non-buggy hook on fresh installs.

## [0.8.0] — 2026-04-27

### Changed

- README.md and docs/QUICKSTART.md updated to promote Obsidian integration and `palinode doctor` as headline features for the v0.8.x rollout.
- `/save` is now the canonical mid-session checkpoint slash command; `/ps` remains as a back-compat alias and `palinode init` scaffolds both.

### Added

- `palinode doctor` across CLI, API, and MCP, including a constrained `--fix` mode for safe setup repairs.
- `docs/DOCTOR.md`, a full diagnostic guide covering the check catalog, `--fix`, and common failure cases.
- `palinode init --obsidian`, `palinode obsidian-sync`, and a broader Obsidian workflow with scaffolding, wiki footer support, and migration guidance.
- `palinode_dedup_suggest` and `palinode_orphan_repair` across CLI, API, and MCP.
- Expanded MCP config diagnostics for Claude Code, Claude Desktop, Cline, Zed, and project-local `.mcp.json`.
- Version-controlled systemd unit templates and an installer under `deploy/systemd/`.
- Search improvements including score-gap dedup, daily-note penalty tuning, canonical-question anchoring, and raw cosine exposure.
- Session-end semantic dedup for recently indexed saves.
- Additional docs around MCP setup and Obsidian workflows.

### Fixed

- `/list` now sorts newest first instead of filesystem glob order.
- `POST /save` now embeds inline before returning, and the watcher verifies vector-index presence before skipping unchanged content.
- Default `db_path` now follows `PALINODE_DIR` overrides more reliably.
- `palinode doctor --json` now emits only JSON on stdout.
- `/health` now reports live chunk and entity counts.
- Fresh-empty-database creation is refused when `memory_dir` already contains memories.
- Startup validation is stricter around `db_path`, `memory_dir`, and reindex concurrency.
- Several timestamp surfaces now use consistent UTC handling.
- `.gitignore` no longer hides legitimate code paths under `palinode/` and `examples/`.
- Worktree test resolution is more reliable for editable installs.

### Removed

- Root-level placeholder systemd unit files superseded by the version-controlled templates and installer under `deploy/systemd/`.

---

## [0.7.2] — 2026-04-26

Bug-fix release with small UX additions. Brings the public repo up to date with two production-impacting fixes (MCP array coercion, SessionEnd hook reasoning) plus search filters and structured session metadata. v0.8.0 remains reserved for a later major feature release.

### Added

- **`/search` filters and recency-only mode (#141)** — `types: [...]` and `since_days: N` are now honored. Empty query routes to a recency-only path that orders by `created_at desc` with no semantic ranking. Backed by a new `store.list_recent()` that pushes type filtering into SQL via `json_extract`. Downstream consumers that wanted "recent Insights" or "recent Decisions from the last 14 days" can now do it in a single call.
- **Structured `session_end` metadata (#145)** — `palinode_session_end` accepts `harness`, `cwd`, `model`, `trigger`, `session_id`, `duration_seconds`. Fields land in the daily-note text and the indexed file's frontmatter. If `project` is omitted but `cwd` is provided, the project slug auto-derives from the cwd's basename via the same slug rules `palinode init` uses.
- **`palinode_save` `ps=true` shortcut (#136)** — MCP parity with the CLI `--ps` flag. `palinode_save` accepts either an explicit `type` or `ps=true` (shorthand for ProjectSnapshot). Conflict between `ps=true` and a non-ProjectSnapshot `type` errors clearly rather than silently ignoring one.
- **Scope frontmatter parsing (#107)** — `scope`, `visibility`, `access` fields recognized in memory frontmatter. Parser-only in this release; search-time use lands in a follow-up.
- **CLI banner** — `palinode --version` and `palinode banner` print the ASCII density-gradient mark.
- **`docs/VALIDATION-STRATEGY.md` (#137)** — formalizes a four-layer model (Tool fired / Data on disk / Retrievable / Behavioral) for validating any cross-session feature, with PR-checklist template. Cross-linked from `docs/INSTALL-CLAUDE-CODE.md`.

### Fixed

- **MCP tolerates JSON-encoded array arguments (#147)** — some MCP transports double-encode array args as JSON strings. Previous behavior rejected with Pydantic 422 ("expected array, received string") on `palinode_session_end` and `palinode_save`, and silently corrupted the `paths` filter on `palinode_diff` (`",".join(string)` was joining char-by-char). New `_coerce_str_array` helper decodes JSON-array strings while passing native lists through unchanged.
- **SessionEnd hook reason filter (#149)** — the hook's README claimed a `clear` matcher that never existed in `settings.json`. Replaced with explicit script-side filtering via a new `PALINODE_HOOK_REASONS` env var. Default allowlist: `clear logout prompt_input_exit other`. Override to narrow (e.g. `"clear"`) or extend (`+resume`, `+bypass_permissions_disabled`).
- **Empty-transcript captures (#151)** — SessionEnd hook was capturing 0-message sessions despite `PALINODE_HOOK_MIN_MESSAGES=3`. Root cause: `grep -c '.'` on empty input emitted `"0"` and exited 1; the `|| echo "0"` fallback then appended another `"0"`, producing the literal three-byte string `"0\n0"` which made the integer test error and fall through. Fix: drop the `|| echo "0"` (grep always emits an integer regardless of exit), use `|| true` to absorb the non-zero, default to `0` if the pipeline produces nothing.
- **Test isolation** — full-suite test runs no longer hit 5 failures in `test_write_time` due to scope-chain reload state pollution.

### Documentation

- **SSH keepalive options for remote-stdio MCP (`docs/MCP-SETUP.md`, `docs/INSTALL-CLAUDE-CODE.md`)** — `ServerAliveInterval=30`, `ServerAliveCountMax=3`, and `TCPKeepAlive=yes` added to the example configs. This helps long-lived SSH-backed MCP sessions survive idle network drops and reconnect more cleanly after laptop sleep or WiFi changes.

### Tests

- 325 tests passing at release time.

---

## [0.5.0] — 2026-04-10

First tagged release. Persistent memory for AI agents with git-versioned markdown as source of truth, hybrid SQLite-vec + FTS5 search, and LLM-driven consolidation applied by a deterministic executor.

### Added

**Core storage and search**
- SQLite-vec vector store with BGE-M3 embeddings (1024d) via any OpenAI-compatible endpoint (Ollama, vLLM, etc.)
- Hybrid search: vector similarity + BM25 (FTS5) fused via reciprocal rank fusion
- Hash-on-read freshness validation — detects out-of-band file edits without a full reindex
- File watcher daemon with debounced reindex and fault isolation

**Consolidation and compaction**
- Deterministic executor applying `KEEP` / `UPDATE` / `MERGE` / `SUPERSEDE` / `ARCHIVE` operations proposed by an LLM (LLM proposes structured operations, deterministic Python applies them — keeps every edit reviewable in git)
- Weekly full-corpus consolidation with configurable LLM backend
- Nightly lightweight consolidation pass (`--nightly` flag) bounded to `UPDATE`/`SUPERSEDE` for safer incremental updates
- Model fallback chains — primary → fallback → fallback on timeout or HTTP error
- Prompt versioning system — extraction/compaction prompts stored as memory files with `active: true` frontmatter

**Interfaces (all four expose the same capabilities)**
- **MCP server** — Streamable HTTP transport (also supports stdio) with 18 tools. Stateless HTTP client, point it at any Palinode API server
- **REST API** — FastAPI on port 6340, 20+ endpoints covering search, save, diff, triggers, history, blame, rollback, consolidation, session-end, lint, migrate
- **CLI** — 26 commands wrapping the REST API via Click. TTY-aware (human output interactive, JSON when piped). Remote access via `PALINODE_API` env var
- **Plugin** — OpenClaw lifecycle hooks for agent frameworks with inject/extract patterns

**New MCP tools in this release**
- `palinode_lint` — scan memory for orphaned files, stale active files, missing frontmatter, potential contradictions
- `palinode_blame` / `palinode_history` / `palinode_rollback` — git-backed provenance tools
- `palinode_trigger` — register prospective triggers that inject memory files when matching context is detected
- `palinode_prompt` — list, read, and activate versioned LLM prompt files

**Capture and session management**
- `/session-end` endpoint — appends session summary to daily notes + one-liner to project status files
- Session-end hook for Claude Code — auto-captures sessions on exit, idempotent, non-blocking
- Entity extraction from daily notes with keyword fallback for untagged content

**Migration**
- `palinode migrate` — import existing markdown memory systems (OpenClaw format) with `--review` mode for dry-run inspection

**Security and hardening**
- Path validation on all file operations (rejects `..`, symlinks outside memory directory)
- Secret scrubbing on save path via configurable regex patterns
- Exclude-paths list prevents search results from surfacing files in `.secrets`, `credentials`, etc.

**Documentation**
- Architecture decision records covering the deterministic executor design
- Remote MCP setup guides for Claude Code, Claude Desktop, Cursor, Zed
- Example memory files (`examples/people/`, `examples/projects/`, `examples/decisions/`, `examples/insights/`)
- Compaction walkthrough (`examples/compaction-demo/`) — a memory file across 3 passes with blame + diff output

**Tests**
- 92 tests covering parser, store, executor, API, CLI, migration, and hybrid search

### Changed
- All inference is local by default. Cloud API keys (Gemini, OpenAI) are opt-in via environment variables
- REST API binds to `127.0.0.1` by default; set `PALINODE_API_HOST=0.0.0.0` to expose on LAN
- FastAPI uses lifespan protocol (deprecated `on_event` removed)
- Git commits stage only `*.md` files, never the SQLite journal/WAL/SHM

### Fixed
- Watcher no longer crashes the API server if the memory directory is temporarily unavailable
- CLI display keys match API response keys across all commands
- Migration tool correctly handles frontmatter with embedded colons

### Removed
- Deprecated SSE MCP transport (replaced by Streamable HTTP per canonical MCP SDK pattern)

---

## [0.1.0] — 2026-03-22

Initial public release. Minimum viable memory system.

### What worked at launch

- SQLite-vec vector store with BGE-M3 embeddings via Ollama
- File watcher daemon auto-indexing markdown on create/modify/delete
- FastAPI server with `/search`, `/save`, `/status`, `/reindex` endpoints
- Markdown parser with YAML frontmatter extraction and heading-level section chunking
- CLI with `search` and `stats` commands
- Plugin with core memory injection, topic-specific retrieval, and three tools (`palinode_search`, `palinode_save`, `palinode_status`)
- Session capture to daily notes on agent end
- Systemd user services for `palinode-api` (port 6340) and `palinode-watcher`

### Architecture decisions

| Decision | Choice | Rationale |
|---|---|---|
| Source of truth | Markdown files, git-versioned | Human-readable, survives everything |
| Vector store | SQLite-vec (embedded) | No server, matches file-based philosophy |
| Embeddings | BGE-M3 via Ollama | Local, private, strong on structured text |
| Prompts | Read from `specs/prompts/*.md` | Version-controlled, editable, diffable |

### Known limitations at 0.1.0

- No WAL mode on SQLite — concurrent writes can lock
- Session capture wrote raw text to daily notes (no LLM extraction)
- No consolidation scheduler
- No entity linking
- No MCP server (plugin-only integration)

All of these are addressed in 0.5.0.
