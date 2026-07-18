# CLAUDE.md — Palinode Memory Integration

Copy this block into your project's `CLAUDE.md` (or run `palinode init` to
scaffold it automatically).

---

## Memory (Palinode)

This project uses Palinode for persistent memory via MCP (server name: `palinode`).

### At session start
- Call `palinode_search` with the current task or project name to pull prior
  context. Surface any relevant decisions, blockers, or insights from previous
  sessions before starting work.
- If the MCP server is down, fall back to the CLI: `palinode search "<query>"`.

### During work
- After each milestone (tests pass, feature shipped, bug root-caused), call
  `palinode_save` with the outcome. Include *why*, not just *what*.
- When making an architectural or design decision, save the decision AND the
  rationale as `type="Decision"`.
- Save surprising reusable findings as `type="Insight"`.
- Every ~30 minutes of active work, save a one-line progress note.

### At session end — including `/clear`
- Call `palinode_session_end` with:
  - `summary` — what was accomplished (1-2 sentences)
  - `decisions` — key decisions made (array of strings, with rationale)
  - `blockers` — open questions or next steps (array of strings)
  - `project` — the project slug
- **`/clear` counts as session end.** Call `palinode_session_end` *before*
  running `/clear`. A SessionEnd hook captures a fallback snapshot, but an
  agent-synthesized summary is far richer than a transcript tail.
- The user may type `/wrap` ("wrap this up") as a shortcut. It is
  **deterministic** — always `palinode_session_end` with
  summary/decisions/blockers, before `/clear`.
- Mid-session checkpoints call the `palinode_save` tool directly with
  `type="ProjectSnapshot"` — there is no separate slash command for them.
  (`/save` and `/ps` are deprecated; existing installs keep working.)

### What NOT to save
- Raw code (git handles that).
- Step-by-step debug logs — save the resolution, not the journey.
- Trivial changes ("fixed typo" is not worth a memory).

### If MCP is not connected
- Use the CLI: `palinode search "<query>"`, `palinode save "<content>" --type Decision`
- Check connection: `palinode status` or the `palinode_status` tool

---

## Getting this set up

Fastest path — from your project root:

```bash
palinode init
```

That scaffolds:

- `.claude/CLAUDE.md` (appends this block if one already exists)
- `.claude/settings.json` (registers the SessionEnd hook)
- `.claude/hooks/palinode-session-end.sh` (auto-captures on `/clear` and exit)
- `.mcp.json` (points Claude Code at the `palinode` MCP server)

Re-run with `--dry-run` to preview, or `--force` to overwrite. See
`examples/hooks/` for the standalone hook files if you prefer a manual setup.

## Obsidian users

If you also use Obsidian, point it at your Palinode directory and you get the graph view, backlinks, and Bases on top of Palinode's hybrid search. Run `palinode init --obsidian /path/to/vault` for the opinionated scaffold. The full guide — quickstart, the wiki-maintenance contract, the embedding tools, and migration paths — is in [`docs/OBSIDIAN.md`](../docs/OBSIDIAN.md).
