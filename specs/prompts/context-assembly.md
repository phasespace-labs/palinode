# Context Assembly Prompt

*Defines how context is assembled for injection at session start. The plugin code implements this logic; this file defines the policy and ranking.*

---

## Phase 1: Core Memory (CAG — before first user message)

Load all files with `core: true` in frontmatter. No search, no ranking — just read them.

**Assembly order:**
1. User profile (people/core.md or equivalent)
2. Active project specs (projects/*/program.md with `core: true`)
3. Standing decisions (decisions with `core: true` and `status: active`)
4. Key people (people files with `core: true`)

**Format as:**
```yaml
# Palinode Core Memory
user:
  name: Paul
  location: Santa Clarita, CA
  roles: [film/video faculty, software developer]

active_projects:
  - name: MM-KMD
    status: M5 Phase 1 complete
    current_work: Voice LoRA deployment on vLLM
  - name: Color Class
    status: Week 8 upcoming
    current_work: RAW/LOG lecture prep
```

Followed by short markdown sections for preferences, decisions, people.

**Budget:** Stay within `coreMemoryBudget` (default 2048 tokens). If over budget:
- Cut oldest non-decision core items first
- Never cut user profile
- Never cut active project status

---

## Phase 2: Topic-Specific Recall (after first user message)

Use the first user message as a search query against SQLite-vec.

**Ranking formula:**
```
score = (vector_similarity × 0.5) + (recency_weight × 0.3) + (importance × 0.2)
```

Where:
- `vector_similarity`: cosine similarity from SQLite-vec (0.0–1.0)
- `recency_weight`: 1.0 for today, decaying to 0.1 for 90+ days old
- `importance`: from frontmatter (default 0.5 if not set)

**Filters applied:**
- Exclude `status: archived` and `status: superseded`
- Prefer items matching detected entities (project names, person names in the message)
- Limit to `searchTopK` results (default 10)

**Grouping (inject as structured sections):**
- **Relevant decisions** (if any match)
- **Recent activity** (project updates, daily notes)
- **People context** (if a person is mentioned)
- **Related insights** (if a topic/pattern matches)

**Budget:** Stay within `topicMemoryBudget` (default 2048 tokens). If over budget:
- Keep highest-scored items per group
- Minimum 1 item per group if any matched
- Truncate long items to first 200 tokens with "[...see full file]" pointer

---

## Hierarchical Expansion

When a chunk matches from SQLite-vec:
1. Always include the source file's YAML frontmatter
2. Include the matched section + one section above and below (sibling context)
3. If the full file is <500 tokens, include the whole file instead of chunks
4. Return file_path so the agent can read the full file via tool if needed

---

## Cold Start (no topic signal)

When there's no first message yet (session just starting):
- Inject Phase 1 only
- Add a single line: "No topic context loaded yet. Will retrieve after first message."
- Phase 2 runs after the first user turn
