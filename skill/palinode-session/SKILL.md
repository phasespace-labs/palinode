---
name: palinode-session
description: "Automatically manage persistent memory during coding sessions via Palinode MCP. Fires when: starting a new task, completing a milestone, making a decision, finishing a session, or when 30+ minutes have passed since last save. Also fires on 'save to memory', 'remember this', 'what do we know about'. Do NOT fire on trivial file edits or routine commands."
---

# Palinode Session Memory

This skill keeps your AI agent's memory fresh across coding sessions using Palinode MCP tools.

## On Session Start

Search for prior context before beginning work:

```
palinode_search(query="[current project or task description]", limit=5)
```

Review results and reference relevant decisions or blockers from previous sessions.

## During Work — Save Milestones

After each major milestone, save the outcome:

```
palinode_save(
  content="[what was accomplished and why]",
  type="Decision",          # or "Insight" for reusable lessons
  project="[project-slug]"
)
```

### When to save:
- Tests pass after a significant change
- Feature is complete and working
- Architectural or design decision made (include rationale)
- Bug fixed that took >15 minutes (save the root cause)
- Something surprising discovered (save as Insight)

### When NOT to save:
- Routine file edits, typo fixes
- Intermediate debug steps (save the resolution only)
- Things git already tracks (code changes, file history)

## Every ~30 Minutes

If actively working and 30+ minutes since last palinode_save, save a brief progress note:

```
palinode_save(
  content="Progress: [what's been done so far, what's next]",
  type="ProjectSnapshot"
)
```

## On Session End

Before the user exits, capture the session:

```
palinode_session_end(
  summary="[1-2 sentence summary of accomplishments]",
  decisions=["decision 1 with rationale", "decision 2"],
  blockers=["open question or next step"],
  project="[project-slug]"
)
```

## Tool Reference

| Tool | When |
|---|---|
| `palinode_search` | Start of session, or "what do we know about X" |
| `palinode_save` | Milestones, decisions, insights, progress |
| `palinode_session_end` | End of session — structured summary |
| `palinode_diff` | "What changed recently?" |
| `palinode_blame` | "When was this decided?" |
| `palinode_trigger` | Register auto-recall for recurring topics |
