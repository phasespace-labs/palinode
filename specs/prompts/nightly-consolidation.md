---
id: prompt-nightly-consolidation
name: nightly-consolidation
task: nightly-consolidation
model: "*"
version: 1
active: true
---

You are updating project status files based on today's session notes.

## Input
- Daily notes from the last 24 hours (session summaries, decisions, blockers)
- Current project status files

## Task
For each project mentioned in the daily notes, propose UPDATE or SUPERSEDE operations on the corresponding status file.

## Rules
- Only UPDATE (append new info) or SUPERSEDE (replace outdated line with current info)
- Do NOT ARCHIVE, MERGE, or KEEP — those are weekly operations
- Each operation targets a specific `id` (fact ID) in the status file
- If a status line is now outdated by today's work, SUPERSEDE it
- If today adds new information, UPDATE with a new status line
- Be concise: one line per status entry, format: `- [YYYY-MM-DD] summary`

## Output
Return a JSON array of operations:
[{"op": "UPDATE", "id": "...", "new_text": "..."}, ...]
