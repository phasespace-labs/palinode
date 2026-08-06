# ADR-001: Tools Over Pipeline

**Status:** Accepted
**Date:** 2026-04-01
**Context:** Claude Mythos announcement, Nate B Jones "compensating complexity" framework

## Decision

Palinode's primary value is its **MCP tool surface + file-based storage + git versioning**, not its 4-phase injection pipeline. The pipeline is scaffolding for current models. The tools survive any model upgrade.

## Context

Nate B Jones' framework distinguishes:
- **Outcome specs** — what you want (survives model upgrades)
- **Procedures** — how to do it (breaks on model upgrades)
- **Tools** — capabilities the model can use (survives)
- **Compensating complexity** — workarounds for model limitations (disposable)

Applied to Palinode:

### What survives any model

The tool surface as it stood when this was written — illustrative, not a census.
It has roughly doubled since; the README's tool reference is the current list.

- `palinode_search` — find relevant memories
- `palinode_save` — store typed memories
- `palinode_session_end` — capture session outcomes
- `palinode_list/read` — browse the memory directory
- `palinode_diff/blame/rollback/push` — git operations
- `palinode_history` — file-level provenance
- `palinode_trigger` — prospective recall
- `palinode_entities` — entity graph
- `palinode_consolidate` — trigger compaction
- `palinode_lint` — structural health checks
- `palinode_ingest` — capture from URLs
- `palinode_status` — system health
- Deterministic executor (validates LLM output, applies ops)
- Markdown files as source of truth
- Git versioning
- Security scanning (business rule)

### What's compensating complexity (today's scaffolding)
- 4-phase auto-injection (Phase 1-4) — compensates for models that don't know to search
- Trivial message skip list — compensates for models that can't judge query relevance
- Layer split keyword heuristics — compensates for models that can't classify by content
- `json_repair` — compensates for models that output malformed JSON
- Explicit JSON format instructions in compaction prompt — same

## Consequences

### Positioning
Lead with: "tools + a memory directory your agent uses however it wants."
Not: "4-phase injection pipeline."

The pipeline is an implementation detail. The tools are the interface.

### Architecture evolution
1. **v0.5 (at time of writing):** Pipeline + tools. Pipeline does automatic injection. Tools available for explicit use.
2. **v1.0:** Pipeline becomes optional/configurable. Default to tools-first for capable models. Pipeline as fallback.
3. **v2.0:** Tools-only mode. Model decides what to retrieve, when to consolidate. Pipeline removed or opt-in legacy.

**Scorecard (added 2026-08).** Step 2 arrived early, at v0.10.0 rather than v1.0:
injection is gated by `auto_inject.enabled` and suppressed per-harness via
`auto_inject.harnesses_disabled`, which defaults to disabling it for the harness
whose own instruction and hook layers already cover session start. Capable clients
are pointed at the tools instead. That is this ADR's central prediction landing —
the pipeline became configurable scaffolding while the tool surface grew.

### What stays regardless of version
- MCP tool interface (the API contract)
- File-based storage (markdown + git)
- Deterministic executor (LLM proposes, executor disposes)
- Security guardrails
- `core: true` flagging (user intent, not model workaround)

### What to audit on each model upgrade
Run the compaction pipeline with prompt sections deleted. Measure what gets worse vs what stays the same. Remove what the new model makes unnecessary.

## References
- Nate B Jones, "Every workaround you built for the last model is now breaking the next one" (April 2026) — the outcome-spec / procedure / tool / compensating-complexity framework this ADR applies
- As of April 2026, the planner–worker–judge split had converged independently across several frontier coding agents; the structural pattern, not any one vendor's implementation, is what this ADR treats as evidence
