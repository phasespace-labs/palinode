# Why Local Memory Matters

AI coding agents are getting powerful automation features — scheduled tasks, looping prompts, MCP integrations, custom skills, computer control. But they all share one limitation: **sessions are stateless by default.**

Every `/clear`, every context compaction, every new session starts from zero. The agent forgets what it did, what it decided, and why.

Cloud vendors will ship memory. It will live on their servers, be opaque, and be locked to their platform. That's convenient — and it's not enough.

## What Palinode does differently

**Files are the source of truth. The LLM is a compiler.**

| Property | Cloud memory | Palinode |
|----------|-------------|---------|
| Storage | Vendor servers | Your machine, your git repo |
| Format | Opaque database | Markdown + YAML frontmatter |
| Auditability | None | `git blame` on every fact |
| Portability | Locked to one platform | Works across any MCP client |
| Ownership | Vendor controls retention | You control everything |
| Offline access | No | `cat` and `grep` always work |
| Consolidation | Black box | Reviewable file updates with git provenance |

## The case for transparency

When an agent remembers something, you should be able to:

1. **Read it** — in a text editor, not a dashboard
2. **Trace it** — `git blame` shows which session recorded each fact
3. **Edit it** — fix mistakes with your editor, not a support ticket
4. **Move it** — switch from Claude to Cursor to Codex without losing memory
5. **Version it** — roll back bad consolidation with `git revert`
6. **Own it** — no vendor lock-in, no data on someone else's server

## Cross-session communication

Palinode turns stateless sessions into a stateful workflow:

- **Session A** saves a decision with rationale → Palinode memory
- **Session B** searches for context → finds the decision, understands why
- **Consolidation** merges overlapping memories → keeps signal high
- **Git** tracks every change → full audit trail

This works across Claude Code, Cursor, Zed, Codex, or any MCP-compatible client. Your memory moves with you.

## When cloud memory is fine

If you use one AI platform, don't need audit trails, and trust the vendor with your data — cloud memory works. Palinode is for people who want to own their agent's knowledge, version it, and take it with them.
