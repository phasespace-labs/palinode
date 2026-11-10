# Why Local Project Memory Matters

Repeated repository work has a simple failure mode: a new session or a different
coding agent lacks the reason a previous approach was accepted or rejected.
Palinode gives that project context a local, inspectable home.

For example, a team can record why it chose SQLite, later record a change to
PostgreSQL, and inspect both records before asking an agent to continue the work.
The later decision does not erase the earlier rationale; it gives people something
specific to review and correct.

## Files, history, and local ownership

Palinode stores memory as Markdown with YAML frontmatter. Git records writes and
later changes, while the search index is derived from the files. This makes the
records readable with ordinary local tools and lets a project keep its own storage
and backup choices.

Local ownership is not a security guarantee by itself. Visibility labels do not
provide encryption or per-user authentication; protect the store, backups, and API
credentials, or use separate instances and filesystem permissions where stronger
separation is needed. See the [privacy contract](PRIVACY.md).

## How a session uses it

An agent can explicitly save a decision, search for it in a later session, and
inspect its source and history. You can open the same records in the read-only
local inspector ([UI.md](UI.md)) and correct or retire one from the agent or the
CLI. Automatic capture and recall are opt-in per client (for example
`palinode init --hook` for Claude Code); installing the MCP server alone does not
enable them. The [Quickstart](QUICKSTART.md) documents the supported first-use
path and its controls; it is the source for what is automatic in a particular
setup.

Consolidation proposes structured changes that Palinode validates and applies.
Review the resulting Git changes before treating them as a correction, and use the
record history to understand what changed.
