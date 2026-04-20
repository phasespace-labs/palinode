---
name: palinode-memory
description: "Search, save, and manage persistent agent memory using Palinode (git-versioned markdown + vector search). Use when user says 'remember this', 'save to memory', 'what do you know about X', 'search memory for context', 'what was decided about', 'register a trigger', 'show memory diff', 'run consolidation'. Also fires when an orchestrating agent needs to: retrieve prior context before a task, save a decision for future sessions, check what is known about an entity, or surface relevant memory before proceeding. Output: structured JSON list with content, category, score fields. Do NOT use for Mem0 queries, session-only notes, or direct file browsing."
metadata:
  author: Paul Kyle
  version: 1.0.0
  mcp-server: palinode
  category: memory
  tags: [memory, persistence, knowledge-management, agents]
  documentation: https://github.com/phasespace-labs/palinode
---

# Palinode Memory

Palinode stores typed memory as git-versioned markdown files with hybrid search (BM25 + vector). This skill provides procedural guidance for using Palinode's tools effectively.

## Core Tools

| Tool | When to Use |
|---|---|
| `palinode_search` | Find relevant memories by meaning or keyword |
| `palinode_save` | Store a new typed memory (person, project, decision, insight) |
| `palinode_status` | Show file counts, chunk counts, entity graph, system health |
| `palinode_trigger` | Register an intention — auto-surfaces a memory file when context matches |
| `palinode_diff` | See what changed in a memory file recently |
| `palinode_consolidate` | Run or check the weekly compaction job |

## Memory Types

Use the right type when saving:

- `PersonMemory` — facts about a person (name, role, preferences, context)
- `Decision` — architectural/product decisions with rationale
- `ProjectSnapshot` — current state of a project (milestone, blockers, next)
- `Insight` — generalizable lessons learned (applies beyond current project)
- `ActionItem` — follow-up task with owner and deadline

## Search Effectively

Palinode uses 4-phase injection — you don't always need to search manually:
1. **Core files** (`core: true`) are always injected at session start
2. **Topic search** runs on every turn automatically
3. **Associative recall** expands entity mentions automatically
4. **Triggers** fire when registered contexts match

Only call `palinode_search` explicitly when you need something specific not surfaced by injection, or when the user asks "what do I know about X?"

## Saving Memories

Guidelines:
- Save **decisions with rationale** — not just what was decided, but why
- Save **people context** when you learn preferences, constraints, or relationships
- Save **insights** when a lesson applies broadly (not just to one task)
- **Don't save** raw conversation transcript — save the distilled fact

Example save calls:
```
palinode_save(
  content="Alice and Bob are co-founders, 3-year gap in joining. CEO role is shared.",
  type="PersonMemory",
  entities=["person/alice", "project/my-app"]
)

palinode_save(
  content="Curation > volume for training data. 90 curated samples >> 1,623 raw.",
  type="Insight",
  entities=["project/my-app"]
)
```

## Triggers

Register an intention when you want a memory to auto-surface in future turns:

```
palinode_trigger(action="add", description="training data curation",
               memory_file="insights/curation-over-volume.md", threshold=0.72)
```

Triggers fire when the user's message semantically matches `description`. Good for:
- "whenever we talk about training data, surface the curation insight"
- "when Alice's design decisions come up, surface the architecture relationships"

## Git Tools

Use these when the user asks about memory history:
- `palinode_diff` — what changed in the last N commits for a file
- `palinode_blame` — who/when each section was written
- `palinode_history` — file change history with diff stats and rename tracking
- `palinode_rollback` — revert a file to a previous state

## Consolidation

Weekly compaction runs Sunday 3am UTC via cron. To run manually:
```
palinode_consolidate(dry_run=True)   # preview what would change
palinode_consolidate(dry_run=False)  # apply operations
```

Operations: KEEP (no change), UPDATE (merge new info), MERGE (combine duplicates), SUPERSEDE (replace fact), ARCHIVE (move to history).

## Setup Reference

See `references/setup.md` for installation, config options, and troubleshooting.
