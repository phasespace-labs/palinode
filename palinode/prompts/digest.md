# Digest & Review Prompts

*Used for generating daily digests and weekly reviews.*

---

## Daily Digest

### Input
- Recent memory operations (from `logs/operations.jsonl`, last 24 hours)
- Active project statuses (files with `core: true` and `category: project`)
- Action items with upcoming due dates (from person and project files)
- Inbox items awaiting review

### Output Format
```
☀️ Palinode Daily — March 22, 2026

📋 Top 3 for today:
1. [action item or focus area]
2. [action item or focus area]
3. [action item or focus area]

🧠 Captured yesterday:
- 2 decisions, 1 project update, 1 person note
- 1 item in inbox awaiting review

⚡ Heads up:
- [upcoming deadline, stale item, or thing that might be stuck]
```

### Rules
- Under 150 words. Phone-screen readable.
- Actionable, not informational. "Review inbox item about X" is better than "You have inbox items."
- If nothing notable happened, send nothing. Don't generate empty digests.

---

## Weekly Review

### Input
- All memory operations from the past 7 days
- All project status changes
- All new decisions
- All new insights
- Inbox items and their resolution status
- Entity activity (who was mentioned, how often)

### Output Format
```
📊 Palinode Weekly — Week of March 17, 2026

🏗️ Projects this week:
- My App: [2-3 sentence status]
- Dashboard: [2-3 sentence status]

🔑 Decisions made:
- [decision 1]
- [decision 2]

💡 Patterns noticed:
- [insight or recurring theme]

⏰ Open loops:
- [unresolved action items or stale things]

🎯 Suggested focus for next week:
- [1-2 things based on what's active and what's stuck]
```

### Rules
- Under 250 words. Scannable in 2 minutes.
- This is a REVIEW, not a report. Synthesize, don't summarize.
- "Patterns noticed" is the most valuable section — this is what the human can't see from daily work.
- Only include projects and people that had actual activity this week.
- If a project went quiet, note that: "My App: no activity this week (last update March 15)."
