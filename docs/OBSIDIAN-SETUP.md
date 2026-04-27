# Using Palinode with Obsidian

The comprehensive guide moved to [OBSIDIAN.md](OBSIDIAN.md) — quickstart, the wiki contract, the embedding tools, migration paths, and FAQ.

The 30-second version: Palinode stores memories as plain markdown with YAML frontmatter, so any folder of Palinode memories is already a valid Obsidian vault. Run `palinode init --obsidian /path/to/vault` to scaffold an opinionated vault config (graph defaults, hidden `archive/` and `logs/`, daily-notes wired to `daily/`), then `open -a Obsidian /path/to/vault` (macOS) or `obsidian /path/to/vault` (Linux). The file watcher handles edits from either side.

For the why, the wiki-contract details, and the embedding tools the LLM calls during maintenance, see [OBSIDIAN.md](OBSIDIAN.md).
