# Using Palinode with Obsidian

Because Palinode stores all memories as standard markdown files, you can use [Obsidian](https://obsidian.md) to visually browse, edit, and link your agent's memories.

## Setup

1. Open Obsidian
2. Click "Open folder as vault"
3. Select your `PALINODE_DIR` (e.g., `~/.palinode`)

## Why This Rocks

- **Visual Graph:** See how your agent connects people, projects, and ideas (frontmatter `entities` arrays act as tags/links if you configure Obsidian's Dataview)
- **Manual Curation:** If the agent gets a detail wrong, just type to fix it. The file watcher will pick up your changes within 2 seconds.
- **Second Brain Integration:** You can symlink specific subdirectories (like `projects/` or `daily/`) into an existing Obsidian vault.
