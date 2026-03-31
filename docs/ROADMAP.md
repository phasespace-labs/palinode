# Palinode Roadmap — Informed by 2025-2026 Agent Memory Research

## Design Philosophy

Palinode occupies a unique position in the agent memory landscape:

| System | Source of Truth | Self-Editing | Config | Open Source |
|--------|----------------|-------------|--------|-------------|
| **Letta** | PostgreSQL + MemFS | Agent rewrites own memory blocks | YAML (.af) | Yes (21K★) |
| **LangMem** | LangGraph Store | Developer-controlled logic | YAML + Pydantic | Yes (1.5K★) |
| **Mem0** | Vector DB + Knowledge Graph | Automatic extraction | API config | Yes (48K★) |
| **OB1 (OpenBrain)** | Supabase (PostgreSQL) | Agent SQL access | MCP config | Yes |
| **Zep/Graphiti** | Temporal graph | Automatic + timeline-aware | API | Partial |
| **Palinode** | Markdown files (git-versioned) | Agent tools + consolidation | YAML | Yes |

**What Palinode does that nobody else does:**
- Files are truth (human-readable, diffable, `cat` always works)
- Git versioning (every memory change has a commit)
- Dual embeddings (private local + cloud for research)
- Graceful degradation (vector index down → files still readable)
- Zero taxonomy burden (system classifies, human reviews)

**What we should learn from others:**

### From Letta: Self-Editing Memory Blocks
Letta agents use `core_memory_replace` to actively update their own context. 
Palinode equivalent: let agents call `palinode_save` with `update_mode: "replace"` 
to overwrite stale project snapshots instead of creating new files.

**Roadmap item:** Add `update_mode` to `palinode_save` (replace/append/create).
**Priority:** High (Phase 1.5)
**Effort:** 2 hours

### From Letta: Read-Only Memory Blocks  
Letta's `read_only: true` prevents agents from overwriting critical rules.
Palinode equivalent: `protected: true` frontmatter flag — agent tools refuse to 
modify these files.

**Roadmap item:** Add `protected` flag to frontmatter schema.
**Priority:** Medium (Phase 2)
**Effort:** 1 hour

### From LangMem: Typed Memory Schemas with Update Modes
LangMem defines `memory_types` with `patch` (update single doc) vs `insert` 
(add to collection). This maps perfectly to Palinode's categories:
- `people/` → patch mode (update the person file)
- `daily/` → insert mode (append new entries)
- `decisions/` → insert with supersession check

**Roadmap item:** Implement `update_mode` per category in config.
**Priority:** High (Phase 1)
**Effort:** 4 hours

### From LangMem: Background Consolidation Manager
LangMem's background manager runs async after sessions to "reflect, extract, 
and consolidate." This is exactly Palinode's Phase 2 consolidation cron.

**Roadmap item:** Implement consolidation cron (already designed in PLAN.md).
**Priority:** High (Phase 2)
**Effort:** 8-12 hours

### From LangMem: Procedural Memory (Prompt Self-Improvement)
LangMem's `create_prompt_optimizer` lets agents update their own system prompt 
based on feedback. Palinode's PROGRAM.md is already designed for this — the memory 
manager reads it before every pass.

**Roadmap item:** Let agents propose edits to PROGRAM.md via tool.
**Priority:** Low (Phase 3)
**Effort:** 4 hours

### From Mem0: Knowledge Graph Entity Linking
Mem0 combines vectors with a knowledge graph for relationship mapping.
Palinode has entity linking in Phase 2 (frontmatter `entities:` field).

**Roadmap item:** Build entity index from frontmatter cross-references.
**Priority:** Medium (Phase 2)  
**Effort:** 6 hours

### From OB1 (OpenBrain): The "Two-Door" Principle
Every memory extension has a "human door" (UI/Slack for manual input) and 
an "agent door" (API/MCP for AI access). Palinode already has this:
- Human door: markdown files, `-es` flag, inbox drop folder
- Agent door: MCP server, OpenClaw tools, FastAPI

**Roadmap item:** Formalize and document the two-door pattern.
**Priority:** Low (documentation)
**Effort:** 1 hour

### From OB1: Domain-Specific Extensions
OB1 has extensions for household, CRM, meal planning, etc. Palinode can adopt 
this pattern: each "extension" is just a new category directory with its own 
schema and extraction rules.

**Roadmap item:** Extension framework (category plugins with custom schemas).
**Priority:** Low (Phase 3+)
**Effort:** 12+ hours

### From Zep/Graphiti: Temporal Memory
Zep tracks WHEN things changed. "User preferred dark mode since March 15."
Palinode has `last_updated` in frontmatter but doesn't expose temporal queries.

**Roadmap item:** Add temporal search (filter by date range, show change history).
**Priority:** Medium (Phase 2)
**Effort:** 4 hours (git log integration)

### From Zep: Contradiction Detection
Zep detects when new facts contradict existing ones. Palinode's consolidation 
prompt mentions supersession but doesn't implement it yet.

**Roadmap item:** Contradiction detection in extraction pipeline.
**Priority:** Medium (Phase 2, with consolidation)
**Effort:** 6 hours

## Prioritized Roadmap

### Phase 1: Config + Quality (THIS EXECUTION)
- [x] `palinode.config.yaml` system with full documentation
- [x] Code quality refactor (docstrings, type hints, comments)
- [x] Bug fixes (#4, #5, #13, #14)
- [x] `__version__`, proper `pyproject.toml`, `py.typed`

### Phase 1.5: Agent Self-Editing (1 week after Phase 1)
- [ ] `update_mode` for palinode_save (replace/append/create)
- [ ] `protected: true` frontmatter flag
- [ ] Update MCP + OpenClaw tools with new parameters

### Phase 2: Consolidation + Entity Linking (2-3 weeks)
- [ ] Weekly consolidation cron
- [ ] Entity index from frontmatter cross-references
| 2 — Consolidation | 📋 Planned | Weekly cron, entity linking, temporal memory, contradiction detection |
| 3 — Migration | 📋 Future | Backfill from Mem0 + QC MCP, pgvector evaluation |
| 4 — Git Tools | ✅ Done | Git memory diff, blame, timeline, rollback |

### Phase 3: Migration + Scale (month 2)
- [ ] Backfill from Mem0 (2,632 memories)
- [ ] Backfill from QC MCP (14K contexts, YOUR_PGVECTOR_SERVER)
- [ ] Evaluate SQLite-vec → pgvector migration
- [ ] Extension framework (custom category schemas)
- [ ] Procedural memory (PROGRAM.md self-editing)

### Phase 4: Git Tools + Federation
- [x] Memory versioning and provenance (`diff`, `blame`, `timeline`)
- [x] Memory protection and recovery (`rollback`, `push`)
- [ ] Multi-agent write access with conflict resolution
- [ ] Cross-instance sync (Palinode on multiple machines)
- [ ] Prompt Pack import/export (OB1-inspired)
- [ ] Public documentation site
