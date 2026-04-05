# CLAUDE.md — Palinode Memory Integration

Add this to any project's CLAUDE.md to enable persistent memory across sessions.

## Memory (Palinode)

This project uses Palinode for persistent memory (MCP server: palinode).

### At session start:
- Call `palinode_search` with the current task or project name for prior context
- Check `palinode_status` if unsure whether the memory system is connected

### During work:
- After each major milestone (tests pass, feature complete, bug fixed, PR ready):
  call `palinode_save` with the decision or outcome
- When making architectural or design decisions:
  call `palinode_save` with the decision AND the rationale (why, not just what)
- Every ~30 minutes of active work: call `palinode_save` with a brief progress note
- When discovering something surprising or reusable:
  call `palinode_save` with type "Insight"

### At session end:
- Call `palinode_session_end` with:
  - `summary`: what was accomplished (1-2 sentences)
  - `decisions`: key decisions made (array of strings)
  - `blockers`: open questions or next steps (array of strings)
  - `project`: project slug if applicable

### What NOT to save:
- Raw code (git handles that)
- Step-by-step debug logs (save the resolution, not the journey)
- Trivial changes ("fixed typo" — not worth a memory)
