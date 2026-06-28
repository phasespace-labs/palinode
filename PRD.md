---
created: 2026-03-22T16:02:00Z
status: draft-v1
author: Paul Kyle + agent
---

# Palinode — Product Requirements Document

**Persistent memory that makes AI agents smarter over time.**

---

## 1. What Is Palinode

Palinode is a memory system for long-lived AI agents. It stores what matters as typed, human-readable markdown files; indexes them for semantic search; injects relevant context at the start of every session; extracts new knowledge at the end of every turn; and consolidates raw captures into curated memory over time.

It is not a knowledge base. It is not a search engine. It is memory — the kind that surfaces without being asked, improves through use, and degrades gracefully when infrastructure fails.

---

## 2. Problem Statement

AI agents wake up with amnesia every session. Current solutions:

| Approach | What It Does | Why It Fails |
|---|---|---|
| **Flat file (MEMORY.md)** | Agent reads one big file at session start | Doesn't scale. 22K tokens, mostly irrelevant. No search. Manual maintenance. |
| **Mem0 (autocapture)** | Extracts facts from conversations into vector DB | Thin snippets without context. No types. No consolidation. 2,632 uncurated memories. Retrieval unreliable. |
| **QC MCP (14K contexts)** | Multi-platform capture into Postgres + pgvector | Overengineered. Goes down. No consolidation. 14K contexts = noise, not memory. Agent can't use it natively. |
| **Conversation history** | Model reads prior turns | Context window limit. Lost on session reset. No persistence. |

All of these store things. None of them *remember*.

---

## 3. Design Principles

1. **Memory = files.** Markdown with YAML frontmatter. Human-readable, git-versioned, greppable. If every service crashes, `cat` still works.
2. **Typed, not flat.** People, projects, decisions, insights — each has a schema. Structure enables reliable retrieval and consolidation.
3. **Consolidation, not accumulation.** 100 sessions should produce 20 well-maintained files, not 100 unread dumps. The system gets smaller and more useful over time.
4. **Invisible when working.** The human talks to their agent. The agent uses Palinode behind the scenes. The only visible outputs are daily digests, weekly reviews, and better conversations.
5. **Graceful degradation.** Vector index down → read files. Embedding service down → grep. Machine off → it's a git repo, clone it anywhere.
6. **Infrastructure-agnostic.** Palinode is a service. OpenClaw is the first client. If the orchestration layer changes, Palinode is portable.
7. **CAG first, RAG at scale.** For small memory (<50 core files), just load the files. No vector search needed. As memory grows, vector search handles scale. The system naturally transitions from "load it all" to "search for relevant chunks."
8. **Zero taxonomy burden.** The human captures. The system classifies, creates entities, maintains the catalog. If the human has to maintain a taxonomy, the system dies.
9. **Nothing hardcoded.** Prompts live in markdown files. Policies live in PROGRAM.md. Thresholds live in config.yaml. The plugin code is plumbing — all behavior is defined in editable, version-controlled text.
10. **Trust through transparency.** Every memory operation is logged. Every file has provenance. Corrections are easy. The system earns trust by being inspectable, not by being perfect.

---

## 4. Users

**Primary:** AI agents (Claude-based) acting as long-lived personal assistants.

**Secondary:** The human (Paul) who browses, edits, and reviews memory files directly; receives daily digests and weekly reviews.

**Tertiary:** Other AI agents that read from shared memory (multi-agent setups).

---

## 5. Features

### 5.1 Memory Store (the filing cabinet)

**What:** Typed markdown files organized by category, with YAML frontmatter for metadata.

**Directory structure:**

```
~/.palinode/
├── people/          → who you know (relationships, prefs, follow-ups)
├── projects/        → what you're building (status, next actions, blockers)
├── decisions/       → choices made (rationale, what was rejected, supersedes)
├── insights/        → lessons learned (patterns, recurring themes)
├── specs/           → living specs and PRDs (the North Star docs)
├── daily/           → raw session logs (ephemeral, feeds consolidation)
├── research/        → reference material with provenance (source, date, author)
├── inbox/           → unclassified captures (confidence < threshold)
├── archive/         → superseded/old items (kept for audit, excluded from search)
└── PROGRAM.md       → drives memory manager behavior (the spec-as-agent pattern)
```

**Frontmatter tags:**

- `core: true` — this file is always loaded at session start (Phase 1 / CAG mode). No vector search needed.
- `status: active | archived | superseded` — controls search visibility and consolidation behavior
- `entities:` — cross-references to other files (auto-maintained by memory manager)

**Schema evolution:** YAML frontmatter is additive. New fields can be added without updating existing files. Missing fields use defaults. Never break backward compatibility.

**File format:**

```yaml
---
id: decision-langgraph-adoption
created: 2026-03-17T14:00:00Z
last_updated: 2026-03-22T15:00:00Z
category: decision
status: active
core: false
entities: [project/my-app]
supersedes: []
confidence: 0.92
source: session/2026-03-17
---

# Decision: Adopt FastAPI for My App Backend

## Statement
Use FastAPI for the backend API in the microservices pivot.

## Rationale
- Async-first design matches our event-driven architecture
- Type hints provide automatic request validation
- OpenAPI spec generation simplifies integration testing

## Alternatives Rejected
- Flask (synchronous, less suited for async workloads)
- Django REST (too heavy for microservice scope)
- Express.js (team more proficient in Python)
```

**Schemas (typed objects):**

| Type | Key Fields | Maps To |
|---|---|---|
| `PersonMemory` | id, name, aliases, role, preferences, relationships, follow_ups, last_contact | `people/*.md` |
| `ProjectSnapshot` | id, name, status, current_work, recent_changes, blockers, linked_decisions | `projects/*.md` |
| `Decision` | id, project_id, statement, rationale, alternatives, supersedes, status | `decisions/*.md` |
| `ActionItem` | description, assignee, due_date, status, related_entities | embedded in project/people files |
| `Insight` | theme, description, evidence_refs, recurrence_count | `insights/*.md` |
| `ResearchRef` | title, source_url, source_file, date, summary, key_points, tags | `research/*.md` |

### 5.2 Hybrid Index (the search layer)

**What:** Vector embeddings combined with a full-text search (BM25) index of all memory files, enabling both semantic search and exact-keyword queries across the entire store.

**Stack:**

- **Vector & Keyword store:** SQLite-vec + FTS5 (embedded, no server, single file at `palinode/.palinode.db`)
- **Embedding model:** BGE-M3 via Ollama (1536d, 8K context, top-tier retrieval)
- **Embedding server:** Ollama (local or remote GPU)

**Indexing:**

- File watcher daemon (`watchdog`) monitors the memory directory
- On file create/modify: parse markdown → split by headings.
- Deduplication: compute `content_hash` (SHA-256) of text. Skip Ollama embedding call if identical to existing hash.
- Insert sections to `chunks` table → auto-syncs to FTS5 virtual table → upserts vectors to SQLite-vec.
- On file delete: remove all references for that file_path
- YAML frontmatter parsed as structured metadata payload (not embedded as text)
- Each vector point carries: `file_path`, `section_id`, `category`, `entity_refs`, `created_at`, `last_updated`, `importance`, `tags`, `status`

**Search:**

- Hybrid search: Reciprocal Rank Fusion (RRF) combines semantic similarity (cosine distance) with BM25 keyword matching + metadata filtering (category, entity, recency, status).
- Hybrid ranking: RRF combined score × recency weight × importance weight (weights configurable in `specs/prompts/context-assembly.md`)
- Exclude `status: archived` from default search results
- Return file paths + section IDs so the agent can read the full file if needed

**Hierarchical retrieval (context expansion):**
When a chunk matches, don't return the chunk alone:

1. Always include the file's YAML frontmatter (structural context)
2. Include adjacent sections from the same file (parent expansion)
3. If the file is short enough and within budget, load the full file
4. Vector search is the first pass; context expansion is the second. Isolated chunks without structure are useless.

### 5.3 Memory Manager (the brain)

**What:** An LLM-powered extraction and update pipeline that runs at the end of every agent turn.

**Extraction (per turn):**

1. Reads `PROGRAM.md` for current behavior instructions (changing PROGRAM.md changes behavior immediately, no restart)
2. Reads extraction prompt from `specs/prompts/extraction.md`
3. Receives last N messages from the conversation
4. Runs typed extraction: returns structured JSON matching schemas
5. Auto-creates entity files for new people/projects on first mention (no human taxonomy work)

**Consolidation also reads PROGRAM.md** at start of each pass for current consolidation rules.

**Update (per candidate):**

1. For each extracted item: search SQLite-vec for similar existing items (same entity/type, top-k)
2. Present old items + new candidate to LLM with tool schema
3. LLM decides: `ADD` (new file/section) | `UPDATE` (modify existing) | `DELETE` (mark archived) | `NOOP` (already known)
4. Apply operation: write/edit markdown file → trigger re-index

**Conflict resolution:**

- Recency wins: `last_updated` field determines which version is current
- Explicit supersession: new Decision that contradicts old → old gets `status: superseded`, new gets `supersedes: [old_id]`
- Both versions kept (audit trail); search layer prefers `status: active`

**Aggressiveness controls** (all defined in PROGRAM.md, not hardcoded):

- Hard cap: max items per turn, max per type (default 5/2 — tunable)
- Significance threshold: only extract decisions, project changes, person context, lessons — not routine Q&A
- Dry-run mode: log candidate items without applying (for tuning)
- Inbox fallback: uncertain classifications → `inbox/` for human review

**Trust mechanisms:**

- **Audit log:** Every ADD/UPDATE/DELETE/NOOP operation logged to `logs/operations.jsonl` with timestamp, source session, confidence, candidate text, and target file
- **Correction flow:** Human says "fix: that decision is wrong" → memory manager re-evaluates → presents options (edit, delete, supersede, reclassify)
- **Configurable receipt:** After each session's extraction, optionally notify the human what was captured (`receiptMode: silent | log | notify`)
- Receipt via daily digest: "Palinode captured 3 items today: 1 decision, 1 project update, 1 person note. 1 item in inbox awaiting review."

**Task prompt capture:**

- Detect when a user message looks like a substantial build spec or research request (length > 500 chars, contains structured instructions, mentions deliverables)
- Offer to save to `specs/task-prompts/{project}/` with date and slug
- Store prompt text + metadata: date, project, model used, output reference
- This preserves the "source code" (the spec) alongside the "compiled output" (the result)

### 5.4 Context Injection (the recall)

**What:** Dynamic assembly of relevant memory injected into the agent's context at session start and after the first user message.

**Phase 1 — Core memory (before first user turn, CAG mode):**

- Load ALL files with `core: true` in frontmatter — no vector search, just read the files
- Typical core files:
  - User profile (`people/core.md`)
  - Active project specs (`projects/*/program.md`)
  - Standing decisions (`decisions/core.md`)
  - Key people index (`people/core.md`)
- Budget: configurable via `coreMemoryBudget` (default ~2K tokens)
- If total core memory exceeds budget, prioritize by: most recently updated → highest importance → alphabetical
- **This is CAG for core memory.** No retrieval latency, no similarity threshold. Just load what matters.

**Phase 2 — Topic-specific recall (after first user message):**

- Use the first message as a search query against SQLite-vec
- Filter by: matching project, matching people, matching topics
- Rank by: vector score × recency × importance
- Group results: facts, preferences, decisions, recent activity
- Budget: ~2K additional tokens
- Inject as structured sections (not flat bullet list)

**Tool-based retrieval (during session):**

- Agent has `palinode_search` tool for on-demand deeper recall
- Triggered when agent detects missing context or ambiguity
- Returns file paths + content sections; agent reads as needed

**Cold start:**

- No topic signal yet → load only Phase 1 (profile + generic preferences)
- First user message triggers Phase 2

### 5.5 Capture (the input)

**Three capture modes, one extraction pipeline:**

**Mode 1: Conversational (automatic + explicit)**

- **Automatic:** `agent_end` hook extracts typed memories from every substantive turn
- **Explicit:** "Remember: Alice wants 5 modules" → classified and filed immediately
- **Channels:** Telegram, Slack, webchat, CLI — all go through OpenClaw → Palinode plugin

**Mode 2: Document ingestion (file drops)**

- Watch folder: `~/palinode-inbox/`, synced to `~/.palinode/inbox/raw/`
- Processing by file type:
  - PDF → text extraction (pymupdf) → LLM summarize + extract → `research/*.md`
  - Audio (m4a/mp3/wav) → Whisper transcription → transcript → summarize → `research/*.md`
  - Video → extract audio → Transcriptor → same as audio
  - Markdown/text → classify directly → appropriate bucket
  - URL (.webloc/.url/text containing URL) → fetch → readability extract → summarize → `research/*.md`
- Each ingested document produces a research reference file with provenance + extracted insights filed into appropriate buckets

**Mode 3: Web capture (URLs)**

- Agent command: "save <https://example.com/article>"
- Fetch → readability extraction → LLM summarize → extract key points → write to `research/`
- Or quick mode: "remember: this article says X about Y" → treated as Mode 1

### 5.6 Consolidation (the sleep)

**What:** Background process that distills raw captures into curated memory. The system gets better, not bigger.

**Weekly consolidation cron:**

**Per project:**

- Collect all `daily/` notes from the past week mentioning this project
- LLM prompt: produce status update (3-7 bullets), key decisions with dates, lessons/insights, unresolved TODOs
- Write to `projects/{id}/summary.md` and `insights/{id}.md`
- Move processed daily notes to `archive/`

**Decision supersession:**

- Scan for new decisions that contradict existing ones (same project + topic)
- LLM comparison: "Does NEW supersede OLD, complement, or contradict?"
- If supersede: mark old as `status: superseded`, link from new via `supersedes: [old_id]`

**Cross-project insights:**

- Feed all recent notes across projects to LLM:
  > "Identify recurring themes. For each: 1-2 sentence description + evidence note IDs."
- Store as `insights/2026-W12.md`

**Entity reference maintenance:**

- Union `entities` lists from source notes into consolidated notes
- Record `source_note_ids` for traceability
- Weekly backward-linking scan: for new entities, search for unlinked references in existing notes

**Archive management:**

- Consolidated daily notes → `archive/daily/`
- Superseded decisions → `status: superseded` (stay in `decisions/`, excluded from default search)
- Truly obsolete items → `archive/` with pointer from main file

### 5.7 Surfacing (the proactive layer)

**Daily digest (morning, via Telegram):**

- Top 3 actions across active projects
- Follow-ups due with people
- One thing that might be stuck
- <150 words

**Weekly review (Sunday evening):**

- What happened across all projects this week
- Stale items (things that haven't moved)
- Recurring patterns the system noticed
- Suggested focus for next week
- <250 words

**Session-start nudge:**

- "You last worked on My App two days ago. Status: M5 Phase 1 complete, waiting on Alice's feedback."
- Injected as part of Phase 1 context

### 5.8 Quality Metrics

**Logged per turn:**

- `session_id`, `turn_id`
- `core_memory_ids_injected`
- `vector_hit_ids` + similarity scores
- `memory_ids_in_prompt`
- User corrections (pattern match: "no that's wrong", "actually we changed X", re-explanations)

**Tracked metrics:**

- **Re-prompt rate:** How often the human re-explains something Palinode should know
- **Recency correctness:** For known-change events, did injected memory reflect the latest version?
- **Over-influence:** Did stale memory override explicit user intent?
- **Token efficiency:** How much of the context budget is used by memory injection?

---

## 6. Data Stack

```
┌─────────────────────────────────────────────────────────────┐
│ SOURCE OF TRUTH                                              │
│                                                              │
│  Markdown files in ~/.palinode/                          │
│  Git-versioned (human-readable, diffable, portable)         │
│  YAML frontmatter for structured metadata                   │
└──────────────────────────┬──────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────┐
│ HYBRID INDEX                                                 │
│                                                              │
│  SQLite-vec + FTS5 (embedded, .palinode.db)                   │
│  - Section-level chunks (512-1024 tokens)                   │
│  - BGE-M3 embeddings (1536d) via Ollama                     │
│  - BM25 full-text search virtual table                      │
│  - Metadata payload: file_path, category, entities,         │
│    created_at, last_updated, importance, status              │
│  - Hybrid search: RRF (vector + keyword) + metadata filters  │
└──────────────────────────┬──────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────┐
│ COMPUTE                                                      │
│                                                              │
│  Memory Manager (Claude via OpenClaw Plugin SDK)            │
│  - Extraction: typed schemas, max 5 items/turn              │
│  - Update: ADD/UPDATE/DELETE/NOOP per candidate             │
│  - Consolidation: weekly cron, LLM-driven merge/supersede   │
│                                                              │
│  File Watcher (Python watchdog, systemd service)            │
│  - Monitors palinode/ → embeds → upserts to SQLite-vec       │
│                                                              │
│  Embedding Generation (Ollama BGE-M3)                    │
└──────────────────────────┬──────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────┐
│ INTERFACES                                                   │
│                                                              │
│  OpenClaw Plugin (openclaw-palinode)                          │
│  - before_agent_start → inject context                      │
│  - agent_end → extract memories                             │
│  - Tools: palinode_search, palinode_save, palinode_ingest         │
│  - CLI: openclaw palinode search/stats/consolidate            │
│                                                              │
│  Capture Points                                              │
│  - Telegram / Slack / webchat / CLI (via OpenClaw)          │
│  - Watch folder (synced to inbox/raw/)                      │
│  - Web capture (agent fetches URL → summarizes → files)     │
│                                                              │
│  Surfacing                                                   │
│  - Daily digest (cron → Telegram)                           │
│  - Weekly review (cron → Telegram)                          │
│  - Session-start nudge (Phase 1 injection)                  │
│                                                              │
│  Future: MCP server for cross-tool access                   │
└─────────────────────────────────────────────────────────────┘
```

---

## 7. Architecture Diagram

```
CAPTURE                          PROCESSING                    MEMORY
───────                          ──────────                    ──────

Telegram ────┐                                                 ~/.palinode
Slack ───────┤                   ┌──────────────┐              ├── people/*.md
Webchat ─────┼→ OpenClaw ──────→│ Palinode Plugin │              ├── projects/*.md
CLI ─────────┘   Agent          │              │              ├── decisions/*.md
                  │              │ before_start │──→ inject    ├── insights/*.md
                  │              │ agent_end    │──→ extract   ├── specs/*.md
                  │              │ tools        │──→ search    ├── daily/*.md
                  │              └──────┬───────┘              ├── research/*.md
                  │                     │                      ├── inbox/
Watch ───────┐   │                     ▼                      ├── archive/
File Drop ───┼───┼──→ Ingestion → Extraction                  ├── .palinode.db
URL Capture ─┘   │    Pipeline    Pipeline                    └── PROGRAM.md
                  │        │           │
                  │        ▼           ▼
                  │    Transcriptor  Memory Manager
                  │                  (typed schemas)
                  │                 ADD/UPDATE/DELETE/NOOP
                  │                     │
                  │                     ▼
                  │              Write markdown + index
                  │
                  └──→ File Watcher Daemon (systemd)
                       watches palinode/ → embed via Ollama → upsert SQLite-vec

Weekly Cron ────→ Consolidation: merge dailies → supersede decisions → extract insights
Daily Cron ─────→ Morning digest → Telegram
Sunday Cron ────→ Weekly review → Telegram
```

---

## 8. Integration with OpenClaw

**Implementation:** OpenClaw Plugin (`openclaw-palinode`)

**Plugin hooks used:**

| Hook | Purpose |
|---|---|
| `before_agent_start` | Inject Phase 1 (core memory) + Phase 2 (topic-specific) context |
| `agent_end` | Extract typed memories from conversation |
| `command:new` / `command:reset` | Trigger full session extraction before context reset |
| `session:compact:before` | Extract from conversation before compaction discards detail |
| `agent:bootstrap` | Inject core memory files into bootstrap (via `bootstrap-extra-files` config) |
| `message:received` | Detect explicit "remember:" prefix captures |
| `gateway:startup` | Initialize SQLite-vec, verify Ollama connectivity |

**Tools registered:**

| Tool | Description |
|---|---|
| `palinode_search` | Semantic + metadata search across all memory files |
| `palinode_save` | Explicit capture — classify and file a thought/fact/decision |
| `palinode_ingest` | Process a URL, file, or document into research + extracted insights |
| `palinode_status` | Show memory stats: file counts, last consolidation, index health |

**CLI commands:**

| Command | Description |
|---|---|
| `openclaw palinode search <query>` | Search from terminal |
| `openclaw palinode stats` | Memory statistics |
| `openclaw palinode consolidate` | Run consolidation manually |
| `openclaw palinode reindex` | Rebuild SQLite-vec from files |

**Config:**

```yaml
extensions:
  openclaw-palinode:
    # Paths
    palinodeDir: "~/.palinode"               # Memory store root
    programFile: "PROGRAM.md"                  # Memory manager behavior spec (relative to palinodeDir)
    promptsDir: "specs/prompts"                # System prompts directory (relative to palinodeDir)

    # Embedding
    ollamaUrl: "http://localhost:11434"       # Ollama endpoint for embeddings
    embeddingModel: "bge-m3"                   # Model name — change without code changes

    # Behavior
    autoCapture: true                          # Extract memories after each agent turn
    autoRecall: true                           # Inject context before each agent turn
    receiptMode: "digest"                      # silent | log | notify | digest

    # Budgets
    coreMemoryBudget: 2048                     # Max tokens for Phase 1 (core/CAG) injection
    topicMemoryBudget: 2048                    # Max tokens for Phase 2 (topic-specific) injection

    # Search
    searchThreshold: 0.6                       # Minimum similarity score for results
    searchTopK: 10                             # Max results per search
    confidenceThreshold: 0.6                   # Below this → inbox for human review

    # Schedules (cron expressions)
    consolidationSchedule: "0 3 * * 0"         # Sunday 3am UTC
    dailyDigestSchedule: "0 14 * * *"          # 7am Pacific (14:00 UTC)
    weeklyReviewSchedule: "0 1 * * 0"          # Sunday 1am UTC

    # Git
    autoCommit: true                           # Commit after extraction/consolidation
    gitRemote: ""                              # Remote for push (empty = no push)
```

All behavior-level configuration (extraction aggressiveness, what to capture, what to ignore, consolidation rules) lives in `PROGRAM.md` and `specs/prompts/*.md`, NOT in this config. Config is for plumbing. PROGRAM.md is for policy.

**Transition from Mem0:**

1. Install `openclaw-palinode` alongside `openclaw-mem0`
2. Both run in parallel — Mem0 continues autorecall, Palinode does its own
3. Agent has both `memory_search` (Mem0) and `palinode_search` (Palinode)
4. Once Palinode proves better retrieval, disable Mem0's autoRecall/autoCapture
5. Eventually remove `openclaw-mem0` extension

---

## 9. Prompts as Source Code

### The Karpathy/YC Parallel

In Karpathy's autoresearch, the most important file is not `train.py` (the code the agent modifies). It's `program.md` (the spec that tells the agent how to think). The human iterates on the spec; the agent iterates on the work. They never touch each other's domain.

```
autoresearch:                          palinode:
  program.md  → agent behavior           PROGRAM.md    → memory manager behavior
  train.py    → the work                 specs/prompts → the executable prompts
  results.tsv → experiment log           quality metrics → experiment log
```

YC/HumanLayer's 12 Factor Agents extends this: **"Your prompts and specs are the source code. Throwing them away after generating output is like compiling Java and checking in the .jar but not the .java."**

This applies at three levels in Palinode:

### Level 1: System Prompts (how the machinery thinks)

The prompts that drive Palinode's behavior — extraction, update, consolidation, context assembly, ingestion, surfacing. These live as editable markdown files, not hardcoded strings.

```
specs/prompts/
├── extraction.md         ← typed extraction prompt
├── update.md             ← ADD/UPDATE/DELETE/NOOP decision prompt
├── consolidation.md      ← weekly merge/supersede logic
├── context-assembly.md   ← how to build session-start injection
├── ingestion.md          ← how to process documents/URLs
└── digest.md             ← daily/weekly review generation
```

The plugin reads prompts from files:

```typescript
// Prompts are files, not strings
const extractionPrompt = fs.readFileSync(
  path.join(palinodeDir, 'specs/prompts/extraction.md'), 'utf-8'
);
```

When you tune extraction, you edit a markdown file. `git log specs/prompts/extraction.md` shows the evolution of how the system learned to think about memory.

### Level 2: Task Prompts (instructions given to agents)

When you spend 30 minutes writing a prompt to an agent — "build a data pipeline spec with Producer/Consumer role structure" — that prompt is the specification. The output is the compiled artifact. Losing the prompt means losing the intent, constraints, and reasoning.

```
specs/task-prompts/
├── my-app/              ← M*-EXECUTE-PROMPT.md files (already doing this!)
├── onboarding/          ← assignment specs, process prompts
└── palinode/              ← research prompts, build prompts
```

The M-EXECUTE-PROMPT.md pattern from My App is already this practice — generalized across all work.

**Capture rule:** When a substantial prompt produces a substantial output, save the prompt alongside the output. The memory manager should detect "this looks like a build spec or research request" and offer to save it to `specs/task-prompts/`.

### Level 3: Meta-Prompts (the system's instructions to itself)

- **PROGRAM.md** — how the memory manager should behave
- **AGENTS.md** — how the agent should behave
- **SOUL.md** — who the agent is

These are already captured as files. They're the highest-level prompts in the system — everything else flows from them.

### The Compounding Loop

```
PROGRAM.md defines behavior
  → specs/prompts/*.md execute that behavior
    → quality metrics measure results
      → human updates PROGRAM.md or prompt files
        → behavior improves
          → next session is better than this one
```

Prompts are not disposable. They're the most durable artifact in the system — more durable than the code that runs them (which can be regenerated from the prompts) and more useful than the outputs (which are just one execution of the spec).

---

## 10. Operational Concerns

**Git automation:**

- After each extraction pass: auto-commit with message `palinode: extracted N items from session {id}`
- After consolidation: `palinode: weekly consolidation {date}`
- `.palinode.db` in `.gitignore` — it's a derived index, rebuildable from files
- `logs/` in `.gitignore` — operational data, not source of truth
- Push to remote: configurable, periodic (daily cron or post-consolidation)

**Startup and health:**

- On startup: verify Ollama is reachable; if not, log warning but continue (files still readable, search degraded)
- File watcher daemon: systemd service with auto-restart
- Health check: `openclaw palinode status` shows: file counts, index freshness, last extraction, last consolidation, Ollama reachability

**Backup:**

- Primary: git remote (GitHub/Gitea/NAS)
- Secondary: files are plain text on disk — any backup tool works (rsync, Syncthing, Time Machine)
- Disaster recovery: clone the repo + `openclaw palinode reindex` rebuilds the vector index from files

**Monitoring:**

- `logs/operations.jsonl` for audit
- Systemd journal for file watcher daemon
- Quality metrics logged per-turn (Section 5.8)

**Schema evolution:**

- YAML frontmatter is additive — new fields don't break old files
- Old files without new fields use defaults
- Never require a migration to add a schema field

**Scope:**

- Single-user by design. Multi-user would require RLS (see OB1's pattern) and is out of scope.

---

## 11. What Palinode Is Not

- **Not a knowledge base.** It doesn't try to store everything. It stores what matters and forgets what doesn't.
- **Not a search engine.** Search is a capability, not the purpose. The purpose is making the agent smarter.
- **Not a notes app.** Humans can read the files, but Palinode is designed for agent consumption first.
- **Not coupled to OpenClaw.** The plugin is an integration layer. The service underneath is portable.
- **Not a replacement for conversation.** Palinode is context, not personality. SOUL.md, AGENTS.md, and the system prompt remain the agent's character.

---

## 12. Evolution from QC MCP

| Dimension | QC MCP (v1, Sept 2025) | Palinode (v2, 2026) |
|---|---|---|
| **Metaphor** | Library (vast, searchable, go to it) | Brain (surfaces what's relevant, consolidates, forgets) |
| **Source of truth** | PostgreSQL rows | Markdown files (git-versioned) |
| **Failure mode** | Server down = memory gone | Files on disk = always accessible |
| **Structure** | Semi-structured (domains, tags, importance) | Typed schemas (Person, Project, Decision, Insight) |
| **Consolidation** | Metabolism concept (keyword matching, never ran in production) | Weekly LLM-driven merge/supersede/archive cron |
| **Agent integration** | MCP bridge (tool the agent calls) | Plugin lifecycle hooks (part of how the agent thinks) |
| **Scale strategy** | Accumulate everything (14K contexts) | Consolidate to what matters (~200 curated files) |
| **Graph** | Separate graph-builder service (batch process) | Frontmatter cross-references (inline, always available) |
| **Retrieval** | Semantic search only | Core memory injection + semantic + metadata + entity matching |
| **Infrastructure** | 4+ services across 3 machines | Files + SQLite-vec + one daemon |

---

## 13. Implementation Phases

| Phase | Scope | Timeline |
| --- | --- | --- |
| **0: MVP** | SQLite-vec + file watcher + session-end extraction + 2 tools + Phase 1 injection | 1 week |
| **0.5: Capture** | Slack channel + Telegram formalization + watch folder + ingestion pipeline | During/after MVP |
| **1: Core Memory** | Two-phase injection + core memory files + retire MEMORY.md | Week 2 |
| **2: Consolidation** | Weekly cron + entity linking + insights extraction | Weeks 3-4 |
| **3: Migration** | Backfill from Mem0 (2,632) + QC MCP (14K) selectively | Week 4+ |
| **4: Multi-Agent + MCP** | Multi-agent read access + MCP server for external tools | Future |

---

## 14. Success Criteria

**After 1 week (MVP):**

- [ ] Agent remembers project state across sessions without MEMORY.md
- [ ] Semantic search returns relevant results for project/people queries
- [ ] Paul re-explains stable facts less often

**After 1 month:**

- [ ] Consolidation produces weekly summaries that are more useful than raw daily notes
- [ ] Daily digest is worth reading 4+ days/week
- [ ] Memory file count grows sub-linearly (consolidation keeps it manageable)
- [ ] Mem0 autorecall disabled — Palinode is strictly better

**After 3 months:**

- [ ] Multiple agents share Palinode (read access for all agent profiles)
- [ ] Palinode has survived at least one infrastructure failure without data loss
- [ ] Paul trusts the system enough to stop manually curating MEMORY.md
