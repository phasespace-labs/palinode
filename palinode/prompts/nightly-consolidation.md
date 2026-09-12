---
id: prompt-nightly-consolidation
name: nightly-consolidation
task: nightly-consolidation
model: "*"
version: 2
active: true
---

You are updating project status files based on today's session notes.

## Input
- Daily notes from the last 24 hours (session summaries, decisions, blockers)
- Current project status files

## Task
For each project mentioned in the daily notes, propose UPDATE, SUPERSEDE or
PROPOSE_CONTRADICTS operations on the corresponding status file.

## Rules
- Only UPDATE (append new info), SUPERSEDE (replace outdated line with current
  info) or PROPOSE_CONTRADICTS (record a conflict, pick no winner)
- Do NOT ARCHIVE, MERGE, or KEEP — those are weekly operations
- Each operation targets a specific `id` (fact ID) in the status file
- If a status line is now outdated by today's work, SUPERSEDE it
- If today adds new information, UPDATE with a new status line
- If a status line and another memory cannot both be true — same subject,
  incompatible values — and today's notes do not say which one replaced the
  other, use PROPOSE_CONTRADICTS instead of SUPERSEDE. It records the conflict
  for human review and retires nothing. SUPERSEDE is the only op that picks a
  winner; use it only when the notes show the change explicitly.
- `contradicts` holds `category/slug` memory refs (for example
  `decisions/deploy-target`), copied exactly as the `ref:` shown for that memory
  in ACTIVE_DECISIONS — never a fact ID, never a title. A ref that does not match
  `category/slug` is rejected; if you cannot name one, propose nothing.
- Be concise: one line per status entry, format: `- [YYYY-MM-DD] summary`

## Output
Return a JSON array of operations:
[{"op": "UPDATE", "id": "...", "new_text": "..."},
 {"op": "SUPERSEDE", "id": "...", "new_text": "...", "reason": "what changed"},
 {"op": "PROPOSE_CONTRADICTS", "id": "...", "contradicts": ["category/slug"], "rationale": "fact says X, that memory says Y, no reversal recorded"}]
