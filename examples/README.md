# Example Memory Files

Copy these to your Palinode memory directory to get started:

```bash
cp -r examples/* ~/.palinode/
```

These are starter templates — edit them with your own information. The file watcher will auto-index them within seconds.

## What's Here

```
people/alice.md       — sample person (collaborator, preferences, context)
projects/my-app.md    — sample project (architecture, milestones, decisions)
decisions/api-design.md — sample decision (what was decided and why)
insights/testing.md   — sample insight (generalizable lesson learned)
```

## File Structure

Every memory file has:
- **YAML frontmatter** — metadata, category, entities, core flag
- **Markdown body** — the actual content, organized by sections
- **Fact IDs** — optional `<!-- fact:slug -->` for compaction targeting

Files marked `core: true` are always injected into agent context. Start with 2-3 core files and expand as needed.
