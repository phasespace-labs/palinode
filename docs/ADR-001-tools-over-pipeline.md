# ADR-001: Tools Over Pipeline

**Status:** Accepted
**Date:** 2026-04-01
**Context:** Claude Mythos announcement, Nate B Jones "compensating complexity" framework

## Decision

Palinode's primary value is its **14 MCP tools + file-based storage + git versioning**, not its 4-phase injection pipeline. The pipeline is scaffolding for current models. The tools survive any model upgrade.

## What survives any model
- MCP tools (search, save, diff, blame, rollback, session_end, trigger)
- Deterministic executor (validates LLM output, applies ops)
- Markdown files as source of truth
- Git versioning
- Security scanning

## What's compensating complexity
- 4-phase auto-injection (compensates for models that don't search proactively)
- Trivial message skip list (compensates for models that can't judge relevance)
- json_repair (compensates for models that output malformed JSON)

## Evolution
1. **v0.5:** Pipeline + tools (current)
2. **v1.0:** Pipeline optional, tools-first default
3. **v2.0:** Tools only, pipeline removed

The less your system prescribes, the more it gains from a smarter model.
