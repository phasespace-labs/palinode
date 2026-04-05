# How Palinode Memory Works

A complete guide to how context flows through the system — from capture to recall to consolidation.

---

## The Memory Lifecycle

```mermaid
graph TD
    subgraph "Real-Time (every turn)"
        MSG[User Message] --> CHECK[Trigger Check]
        CHECK -->|Fired| INJECT
        CHECK --> RECALL{Recall Engine}
        RECALL -->|Phase 1| CORE[Core Memory<br>core:true files]
        RECALL -->|Phase 2| SEARCH[Hybrid Search<br>BM25 + Vector]
        RECALL -->|Phase 3| ASSOC[Associative Search<br>Entity Graph]
        CORE --> INJECT[Context Injection<br>&lt;palinode-memory&gt;]
        SEARCH --> INJECT
        ASSOC --> INJECT
        INJECT --> AGENT[Agent Response]
        AGENT --> CAPTURE[Session Capture<br>→ daily/YYYY-MM-DD.md]
    end

    subgraph "Weekly (Sunday 3am)"
        DAILY[daily/*.md] --> CONSOLIDATE{Consolidation<br>OLMo 3.1}
        CONSOLIDATE --> SUMMARIES[Project Summaries<br>projects/*.md]
        CONSOLIDATE --> DECISIONS[Decision Updates<br>decisions/*.md]
        CONSOLIDATE --> INSIGHTS[Cross-Project Insights<br>insights/*.md]
        CONSOLIDATE --> ARCHIVE[Archive Processed<br>archive/YYYY/]
    end

    subgraph "On Demand"
        ES[-es Quick Capture] --> ROUTE{Smart Router}
        ROUTE -->|URL| INGEST[Ingest → research/]
        ROUTE -->|Long text| RESEARCH[ResearchRef]
        ROUTE -->|Short text| INSIGHT[Insight]
        SAVE[palinode_save] --> FILES[Memory Files]
        INGEST_TOOL[palinode_ingest] --> RESEARCH
    end

    CAPTURE --> DAILY
    FILES --> WATCHER[File Watcher]
    WATCHER --> INDEX[SQLite-vec + FTS5]
    INDEX --> SEARCH
```text

---

## 1. Session Recall (Every Agent Turn)

**Hook:** `before_agent_start` in the OpenClaw plugin

Every time you send a message, Palinode injects relevant context **before the agent sees your message**. This happens in four phases:

### Phase 1: Core Memory (always injected)

Core memory files are marked with `core: true` in their YAML frontmatter. These are the facts the agent should always know — who you are, what you're working on, key decisions.

**Smart injection — only when needed:**

| When | What's Injected | Tokens |
| --- | --- | --- |
| **Turn 1** (session start) | Full content of all core files (up to 8K total) | ~2,000 |
| **After compaction** | Full content again (context was just summarized) | ~2,000 |
| **All other turns** | Controlled by `mid_turn_mode` (default: nothing) | 0 |
| **Fallback** (every 200 turns) | Full content (last resort if compaction hook fails) | ~2,000 |

Core memory persists in the model's context window from turn 1 until OpenClaw compacts the session. The compaction hook (`after_compaction`) detects when context was summarized and triggers a full re-injection on the next turn. This eliminates periodic timer-based re-injection, saving ~50K tokens per long session.

**Currently ~6 core files:**

- `people/paul.md` — who you are
- `people/peter.md` — your collaborator
- `projects/mm-kmd.md` — current project status
- `projects/palinode.md` — memory system status
- `projects/color-class.md` — teaching schedule
- `projects/infrastructure.md` — homelab machines and services

### Phase 2: Topic-Specific Search (per message)

After core injection, Palinode searches for context relevant to **what you just said**.

- Uses hybrid search: BM25 keyword matching + BGE-M3 vector similarity + RRF merge
- Results are adjusted by **Temporal Decay**, bumping up scores for recently updated and highly important memories.
- Returns top 5 results, 700 chars each
- **Skipped for trivial messages** (< 15 chars, or acks like "ok", "sure", "thanks")
- Searches across all memory types: projects, decisions, insights, daily notes, research

### Phase 3: Associative Context (Spreading Activation)

If your message discusses known entities (people, projects), Palinode searches the entity graph to find related files that share those entities. This surfaces horizontally related information that might not match exact keywords or semantic vectors, but is structurally related to the topic. Up to 3 related files are added.

### Phase 4: Prospective Triggers

Palinode maintains a background index of "triggers" (specific situational contexts). Every message is checked against this list. If the semantic meaning of your message matches a trigger description, the associated memory file is forcibly injected into the context. This allows the agent to essentially leave a "note to self" to remember a specific file the next time a specific situation arises.

**What the agent sees (wrapped in `<palinode-memory>` tags):**

```xml
<palinode-memory>
## Core Memory

--- people/alice.md ---
> Alice Chen — Product lead at Acme Corp. Prefers async communication. Working on the mobile checkout redesign.

--- projects/checkout.md ---
> Mobile checkout redesign. v2 on React Native...

## Relevant Context
[decisions] MM-KMD will use 5 acts instead of 3. Peter's creative direction...
[insights] For LoRA training, 90 curated samples outperform 1,623 raw...
</palinode-memory>
```text

### Sensitive Content Scrubbing

Before injection, all content passes through `specs/scrub-patterns.yaml` — regex patterns that redact credentials, phone numbers, and other PII. The agent never sees raw secrets.

---

## 2. Session Capture (End of Every Turn)

**Hook:** `agent_end` in the OpenClaw plugin

After each agent response, the plugin captures the conversation to a daily note:

1. Takes the last 10 messages (user + assistant)
2. Strips any `<palinode-memory>` tags (avoids feedback loop — don't store the injection itself)
3. Caps at 2,000 characters
4. Appends to `daily/YYYY-MM-DD.md`

**What a daily note looks like:**

```markdown
## Session 2026-03-29T04:26:16Z

user: what's the status on Palinode?

assistant: Phase 2 complete. Consolidation cron enabled, entity linking live,
temporal search working. 2,165 chunks indexed across 219 files...
```text

Daily notes are **raw session logs** — unprocessed, append-only. They accumulate throughout the week and are distilled by the consolidation cron.

### Context Reset Capture

**Hook:** `before_reset`

When you run `/new` (session reset), the plugin flushes the last 20 messages to daily notes before the context is cleared. This ensures nothing is lost during a reset.

---

## 3. Quick Capture (`-es` flag)

**Hook:** `message_received`

Append `-es` to any message to save it directly to Palinode:

```text
Alice wants async check-ins instead of meetings -es
```text

**Smart routing by content type:**

| Input | Detected As | Destination |
| --- | --- | --- |
| `https://example.com -es` | Bare URL | Fetches page → `research/` |
| Long text (>500 chars) `-es` | Article/notes | `research/` as ResearchRef |
| Short text + URL `-es` | Note with source | Saves note + fetches URL |
| Short text `-es` | Quick fact | `insights/` |

**Safety checks:**

- Won't fire inside code blocks
- Requires word boundary (won't match mid-word)
- Minimum 5 characters
- URL regex strips trailing punctuation

**Receipt:** On the next turn, Palinode injects `*[Saved to Palinode: {slug}]*` into the context so the agent can acknowledge the capture naturally.

---

## 4. Weekly Consolidation (Sunday 3am UTC)

**Script:** `palinode/consolidation/runner.py`  
**LLM:** OLMo 3.1:32b on vLLM (localhost:8000)  
**Schedule:** `0 3 * * 0` (crontab)  
**Prompt:** `specs/prompts/consolidation.md`

The consolidation cron is where raw daily logs become curated memory.

### What It Does

```mermaid
graph LR
    D[daily/*.md<br>7 days] --> COLLECT[Collect Notes]
    COLLECT --> GROUP[Group by Project<br>entity tags + keywords]
    GROUP --> LLM[OLMo 3.1<br>Distill per project]
    LLM --> WRITE[Update project summary]
    LLM --> DECISIONS[Detect superseded decisions]
    LLM --> INSIGHTS[Extract cross-project insights]
    D --> ARCHIVE[Move to archive/YYYY/]
```text

### Step by Step

1. **Collect** — reads all `daily/YYYY-MM-DD.md` files from the last 7 days
2. **Group** — assigns notes to projects by:
   - Entity tags in frontmatter (`entities: [project/mm-kmd]`)
   - Keyword fallback (scans content for project names, tool names, etc.)
3. **Analyze** — for each project, sends notes + current summary + existing decisions to the LLM (OLMo 3.1:32b) with the compaction prompt to determine what facts are relevant
4. **Determine Operations** — the LLM returns structured JSON operations (`KEEP`, `UPDATE`, `MERGE`, `SUPERSEDE`, `ARCHIVE`) determining the fate of each active fact
5. **Execute Compaction** — the Compaction Executor runs deterministically to modify or move facts:
   - Updated/Merged facts are preserved in the Identity or Status layers.
   - Superseded or Archived facts are moved to the History layer (`{name}-history.md`) with a rationale and timestamp ensuring data is never lost.
6. **Assign IDs** — any newly generated facts get a deterministic `<!-- fact:slug -->` ID block for tracking.
7. **Insights** — runs all notes (not per-project) through the insight extraction prompt, looking for cross-project patterns
8. **Archive** — moves processed daily notes to `archive/YYYY/`
9. **Commit** — `git commit -m "palinode: weekly consolidation {date}"`

### Token Budget

Each project compaction is capped at ~6,000 chars of daily notes (1,500 per note, most recent first). Combined with the existing structured facts, this fits comfortably within OLMo's 4,096 token context without losing detail.

### What It Produces

**Before consolidation (daily/2026-03-29.md):**

```text
## Session 2026-03-29T04:26:16Z
user: completed M5 Phase 4 tests.
assistant: Updating MM-KMD with testing progress.

## Session 2026-03-29T16:12:25Z  
user: run the consolidation
assistant: Processed 18 notes, MM-KMD summary updated via 5 KEEP, 2 UPDATE, 1 ARCHIVE ops...
```text

**After consolidation (projects/mm-kmd-status.md):**

```text
## Active Milestones
- <!-- fact:m5-completed --> [2026-03-29] M5 Phase 4 tests completed successfully.
```text

**Archived directly to (projects/mm-kmd-history.md):**

```text
## Archived Facts
- <!-- fact:m5-in-progress --> [2026-03-25] Working on M5 Phase 4.
  *(Archived 2026-03-29: Superseded by m5-completed)*
```text

---

## 5. File Indexing (Continuous)

**Service:** `palinode-watcher` (systemd, watchdog library)

The file watcher monitors the entire memory directory. When any `.md` file is created, modified, or deleted:

1. **Parse** — split markdown into sections by heading
2. **Hash** — SHA-256 each section's content
3. **Skip if unchanged** — compare hash to existing index entry
4. **Embed** — send to Ollama BGE-M3 (1024d vectors)
5. **Upsert** — store in SQLite-vec (vector) and FTS5 (keyword)
6. **Entity index** — extract `entities:` from frontmatter, update entity table

### Content-Hash Deduplication

Each chunk is hashed before embedding. If the hash matches the existing entry, the ~200ms Ollama API call is skipped entirely. On a full reindex of 2,000+ chunks where most are unchanged, this saves ~90% of embedding calls.

### What Gets Indexed

| Directory | Indexed | Why |
| --- | --- | --- |
| `people/` | ✅ | Person memory |
| `projects/` | ✅ | Project snapshots |
| `decisions/` | ✅ | ADRs and choices |
| `insights/` | ✅ | Lessons learned |
| `daily/` | ✅ | Session logs |
| `research/` | ✅ | Reference material |
| `archive/` | ❌ | Processed, excluded |
| `inbox/processed/` | ❌ | Processed drops |
| `.git/` | ❌ | Git internals |
| `venv/`, `node_modules/` | ❌ | Build artifacts |

---

## 6. Git Versioning (Every Change)

**Repo:** `Paul-Kyle/palinode-data` (PRIVATE)

Every memory change is a git commit. This enables:

| Tool | What It Does | Example |
| --- | --- | --- |
| `palinode_diff` | What changed recently? | "Show me changes this week" |
| `palinode_blame` | When was this fact recorded? | "When did Alice mention async?" |
| `palinode_timeline` | How has this file evolved? | "Show MM-KMD's history" |
| `palinode_rollback` | Undo a bad change | "Revert last consolidation" |
| `palinode_push` | Sync to GitHub | "Backup my memory" |

### Auto-Commit Points

| Event | Commit Message |
| --- | --- |
| `palinode_save` | `palinode auto-save: {category}/{slug}.md` |
| `-es` capture | `palinode auto-save: {category}/{slug}.md` |
| Consolidation | `palinode: weekly consolidation {date}` |
| Rollback | `palinode: rollback {file} to {commit}` |
| Migration | `palinode: Mem0 backfill — N files from M memories` |

---

## 7. Entity Linking

Every memory file can reference entities via the `entities:` frontmatter field:

```yaml
entities: [person/alice, project/checkout]
```

The entity index is a reverse lookup: given an entity, find all files that mention it.

**API:** `GET /entities/person/alice` → returns all files referencing Alice

**Entity graph:** shows which entities co-occur. If `person/alice` and `project/checkout` always appear together, the system knows they're related.

**Currently 20 entities tracked** across 219 files.

---

## 8. Behavioral Spec: PROGRAM.md

`PROGRAM.md` is the behavioral specification for the memory manager. It controls:

- What to extract (and what to ignore)
- Aggressiveness thresholds
- Consolidation rules
- Quality standards

The consolidation runner uses the prompt in `specs/prompts/compaction.md`. To change consolidation behavior, edit that file — no code changes needed. (PROGRAM.md documents overall agent behavior, not the consolidation runner specifically.)

---

## Summary: What Happens When

| Event | What Palinode Does |
| --- | --- |
| **You send a message** | Injects core memory + topic-relevant context |
| **Agent responds** | Captures last 10 messages to daily notes |
| **You type `-es`** | Saves immediately, routes by content type |
| **You call `palinode_save`** | Writes typed markdown file, auto-commits |
| **A file is saved** | Watcher embeds + indexes it |
| **Sunday 3am** | Consolidation distills weekly notes into summaries |
| **You ask "what changed?"** | `palinode_diff` shows git diff of memory files |
| **You ask "when did I learn X?"** | `palinode_blame` traces to the commit |

---

## 9. Memory Provenance (The Git Chain)

Every fact in Palinode has a traceable origin. Here's how a memory evolves:

```text
Feb 11  — Mem0 auto-captured: "max_tokens scaling reduces dialogue length"
          (Qdrant, no provenance, no context)

Mar 29  — Backfilled to Palinode: classified by Qwen 72B, written to
          projects/mm-kmd-milestones.md (commit dcdbf5f)

Apr 06  — Weekly consolidation: OLMo distilled 7 days of session notes,
          updated mm-kmd.md with new status bullets (commit abc1234)

Apr 13  — Manual edit: Alice corrected a fact via palinode_save (commit def5678)
```text

**`palinode_blame`** shows both the git date AND the true origin date:

```text
## Blame: projects/mm-kmd-milestones.md
Origin: 2026-02-11 | Source: mem0-backfill
Note: Git shows 2026-03-29 (migration date). True origin is 2026-02-11 (from mem0-backfill).

^dcdbf5f (2026-03-29) - [2026-02-11] M5 Phase 1 complete: 9 voice LoRAs deployed
^dcdbf5f (2026-03-29) - [2026-02-15] M2 closed: memory + personality systems
abc1234  (2026-04-06) - M6 Phase 1 spec ready: gravity routing fixed
def5678  (2026-04-13) - M6 Phase 2: Scene Conscience beat detection live
```text

For backfilled memories, git blame shows when the file was migrated. The frontmatter `created_at` field preserves the true origin date from the source system (Mem0, QC MCP, etc.). Palinode surfaces both so you always know:

- **When the fact was first captured** (frontmatter `created_at`)
- **When this file was last modified** (git blame date)
- **Where the memory came from** (frontmatter `source`)

For memories captured natively by Palinode (not backfilled), both dates match.

**`git log --follow`** shows the complete history of a file — every consolidation, every manual edit, every backfill.

### Backfill Provenance

Palinode has already absorbed memories from two external systems:

| Source | Memories | Classified By | Status |
| --- | --- | --- | --- |
| **Mem0** (Qdrant) | 4,637 → 3,645 (after dedup + skip) | Qwen 72B | ✅ Done |
| **QC MCP** (PostgreSQL) | 14,000+ contexts | TBD | Planned |

Backfilled memories enter `palinode-data` with `source: "mem0-backfill"` in their frontmatter. As consolidation updates them, each change gets its own commit — gradually building provenance that Mem0 never had.

### Why This Matters

Other memory systems are opaque databases. You can query them but you can't ask:

- "When did I first learn that Alice prefers async?" → `palinode_blame`
- "What changed about the MM-KMD project this week?" → `palinode_diff`
- "Show me every update to my infrastructure notes" → `palinode_timeline`
- "The last consolidation was bad, undo it" → `palinode_rollback`

These aren't add-on features. They're consequences of the architectural decision to use files + git as the source of truth. The audit trail is free.
