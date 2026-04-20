---
name: palinode-claude-code
description: "Use Palinode persistent memory in Claude Code via MCP (13 tools for search, save, git diff, triggers, consolidation). Use when user says 'save this decision', 'remember why we chose X', 'search memory before we start', 'what do we know about this project', 'show what changed in memory', 'register a trigger for this topic', 'set up Palinode MCP'. Also fires when an agent needs to: load prior context at session start, persist an architectural decision, retrieve past decisions before writing code, or surface relevant memory before proceeding with a task. Output: structured JSON. Do NOT use for Mem0, session-only notes, or running the Palinode API server."
metadata:
  author: Paul Kyle
  version: 1.0.0
  mcp-server: palinode
  category: memory
  tags: [memory, persistence, claude-code, mcp, knowledge-management]
  documentation: https://github.com/phasespace-labs/palinode/blob/main/docs/INSTALL-CLAUDE-CODE.md
---

# Palinode — Claude Code Integration

Palinode gives Claude Code persistent, git-versioned memory via 13 MCP tools. Memories survive across sessions, are searchable by meaning, and consolidate weekly.

## Session Workflow

### Before Starting Work
```
Use palinode_search to find any prior context on [project/topic]
```
Surfaces relevant decisions, people context, and insights automatically.

### During Work — Save Key Decisions
When you make an architectural or product decision:
```
palinode_save: decided to [X] because [Y]. trade-off was [Z].
type: Decision
entities: [project/name]
```

### After a Long Session
```
Save key decisions and insights from this session to palinode
```

---

## Core Tools Quick Reference

| Tool | Call Pattern |
|---|---|
| Search | `palinode_search(query="...", limit=5)` |
| Save decision | `palinode_save(content="...", type="Decision", entities=["project/name"])` |
| Save insight | `palinode_save(content="...", type="Insight")` |
| Save person | `palinode_save(content="...", type="PersonMemory", entities=["person/name"])` |
| Status | `palinode_status()` |
| Recent changes | `palinode_diff(file="projects/name.md", n_commits=3)` |
| Register trigger | `palinode_trigger(action="add", description="...", memory_file="path/to/file.md")` |
| Consolidate | `palinode_consolidate(dry_run=True)` |

## Memory Types

- `Decision` — what was decided and why (rationale is critical)
- `Insight` — generalizable lessons (applies beyond one project)
- `PersonMemory` — person's role, preferences, constraints
- `ProjectSnapshot` — current state (milestones, blockers, next steps)
- `ActionItem` — follow-up with owner and deadline

## When to Save vs. Not

**Save:**
- Architectural decisions with rationale
- Bugs that took >30min to find (so future-you recognizes them)
- Performance findings (e.g., "bge-m3 cold start is 30s, warm is 100ms")
- People context (preferences, constraints, relationships)
- Lessons that apply broadly across projects

**Don't save:**
- Raw code (git handles it)
- Step-by-step log of what you did (save the *outcome*, not the journey)
- Temporary debug notes

## Setup

See `references/setup.md` for MCP config, SSH remote setup, LaunchAgent, and troubleshooting.

## Triggers

Register once, fires automatically whenever the context matches:

```python
palinode_trigger(
    action="add",
    description="training data curation",
    memory_file="insights/curation-over-volume.md",
    threshold=0.72
)
```

Good patterns:
- Project-specific risks → `"when working on [project], surface [file]"`
- Recurring gotchas → `"when deploying models, surface memory/oom-lessons.md"`
- People context → `"when Alice's design decisions come up, surface people/alice.md"`
