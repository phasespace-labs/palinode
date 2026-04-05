# Git-Powered Memory in Palinode

Palinode treats memory as code. Every fact, decision, and project detail your agent learns is saved as a markdown file and versioned in a dedicated Git repository (`palinode-data`). This provides complete provenance: you can trace any fact back to the exact session where it was recorded, and see how your agent's understanding evolved over time.

## Core Concepts

Because the memory directory is just a Git repository, Palinode provides built-in tools to inspect it without requiring you to use the Git CLI manually. These tools are available via:
- **CLI Commands:** for human administrators.
- **MCP Tools:** for LLMs via Claude Code.
- **OpenClaw Plugin:** read-only tools for the chat agent.

### 1. Diff

Show what memory has changed recently. This is the best way to review what your agent has learned over the past week.

```bash
palinode diff --days 7
```

### 2. Blame (with Origin Provenance)

Find out *when* and *why* a specific fact was recorded. Palinode's blame shows **two dates**: the git commit date (when the file was last touched) and the frontmatter origin date (when the memory was first captured).

This is critical for backfilled memories: a fact captured by Mem0 on February 11th and migrated to Palinode on March 29th shows both dates:

```bash
palinode blame projects/mm-kmd-milestones.md --search "LoRA"
```
```
## Blame: projects/mm-kmd-milestones.md
Origin: 2026-02-11 | Source: mem0-backfill
Note: Git shows 2026-03-29 (migration date). True origin is 2026-02-11 (from mem0-backfill).

^dcdbf5f (2026-03-29) - [2026-02-15] M5 Phase 1 complete: all 9 voice LoRAs deployed
```

For natively-captured memories, both dates match:
```bash
palinode blame decisions/kmd-five-acts.md --search "5 acts"
# abc1234 (2026-04-06) Peter wants 5 acts instead of 3
# (origin: 2026-04-06, source: palinode — dates match)
```

### 3. Timeline

Watch a structured memory evolve. Timeline shows all changes to a specific file, ordered chronologically.

```bash
palinode timeline projects/mm-kmd.md
```

### 4. Rollback (Admin Only)

If an agent mistakenly consolidates or overwrites data, you can revert the file. Palinode's rollback is safe: it creates a *new* commit that restores the file, preserving the erroneous history just in case.

```bash
palinode rollback projects/mm-kmd.md --commit a1b2c3d
```

> **Note:** Rollback defaults to a dry run. To actually apply the change, you must pass `--execute`.

### 5. Push (Admin Only)

Sync your agent's memory to GitHub. This serves as a backup and allows you to sync memories across multiple machines (by pulling the data repo elsewhere).

```bash
palinode push
```

## Security

By design, destructive Git operations (`rollback` and `push`) are restricted to the CLI and MCP tools. They are **not** exposed in the OpenClaw chat plugin to prevent the agent from accidentally or maliciously reverting files or pushing incomplete data to the remote origin.
