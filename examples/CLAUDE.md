# CLAUDE.md — Palinode Memory Integration

Copy this block into your project's `CLAUDE.md` (or run `palinode init` to
scaffold it automatically).

---

## Memory (Palinode)

This project uses Palinode for persistent memory via MCP (server name: `palinode`).

### At session start
- Call `palinode_search` with the current task or project name to pull prior context.
- If the MCP server is down, fall back to the CLI: `palinode search "<query>"`.
- Records from other projects come back only when you pass `include_other_projects`, and they are context only, never this project's decisions. Don't opt in to find a value this project's memory lacks; ask the user instead.

### During work
- When a value, ID, name, decision or plan is settled (for example a batch ID fixed or an approach agreed), save it right away with `palinode_save` as `type="Decision"`, with the value and why. Don't wait for the end of the session.
- After a milestone (tests pass, feature shipped, bug root-caused), call
  `palinode_save` with the outcome. Include *why*, not just *what*.
- When making an architectural or design decision, save the decision AND the
  rationale as `type="Decision"`.
- Save surprising reusable findings as `type="Insight"`.
- After roughly 30 minutes, save a progress note only if new durable progress
  has accumulated. Skip timer-based writes during recall-only work.
- `palinode_save` and `palinode_session_end` are explicit, content-bearing
  writes. They are separate from any opt-in client hook and from background
  auto-summary enrichment.

### At session end — including `/clear`
- At a user-requested wrap-up, or when the session produced new durable
  information, call `palinode_session_end` with `summary`, `decisions`,
  `blockers`, and `project="<your-project-slug>"`.
- Do not create a recap for a recall-only or no-new-information session, and
  honor an explicit request not to save.
- `/clear` by itself does not override that choice. The SessionEnd hook
  installed by `palinode init --hook` is an opt-in automatic fallback;
  plain `palinode init` does not enable transcript capture.
- The user may type `/wrap` (session wrap-up) as a shortcut. It is
  an explicit request to capture: perform its deterministic
  `palinode_session_end` with summary/decisions/blockers before `/clear`,
  unless the user separately says not to save. See the `/wrap` command/skill
  definition (installed by `palinode init`) for the exact prompt.
- Mid-session checkpoints go through the `palinode_save` tool directly
  (`type="ProjectSnapshot"`); there is no separate slash command for them.
  (`/save` and `/ps` are deprecated — existing installs keep working.)

### What NOT to save
- Raw code (git handles that).
- Step-by-step debug logs — save the resolution, not the journey.
- Trivial changes ("fixed typo" is not worth a memory).

### Project slug
This project's slug is `<your-project-slug>`. Pass it as the `project` argument to
`palinode_save` and `palinode_session_end` so status rolls up correctly.

### If MCP is not connected
- Use the CLI: `palinode search "<query>"`, `palinode save "<content>" --type Decision`
- Check connection: `palinode status` or the `palinode_status` tool

---

## Getting this set up

Fastest path — from your project root:

```bash
palinode init --hook
```

That scaffolds:

- `.claude/CLAUDE.md` (appends this block if one already exists)
- `.claude/settings.json` (registers lifecycle hooks only with `--hook`)
- `.claude/hooks/palinode-session-end.sh` (auto-captures on `/clear` and exit)
- `.claude/hooks/palinode-post-compact.sh` (saves Claude Code's compaction summary as a checkpoint)
- `.mcp.json` (points Claude Code at the `palinode` MCP server)

Plain `palinode init` does not enable transcript capture. Run `palinode controls
status` before opting in to inspect control state and redacted effective
destinations. Re-run with `--dry-run` to preview, or `--force` to overwrite. See
`examples/hooks/` for the standalone hook files if you prefer a manual setup.

## Obsidian users

If you also use Obsidian, point it at your Palinode directory and you get the graph view, backlinks, and Bases on top of Palinode's hybrid search. Run `palinode init --obsidian /path/to/vault` for the opinionated scaffold. The full guide — quickstart, the wiki-maintenance contract, the embedding tools, and migration paths — is in [`docs/OBSIDIAN.md`](../docs/OBSIDIAN.md).
