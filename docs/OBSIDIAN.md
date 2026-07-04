# Palinode + Obsidian

Point Obsidian at your Palinode directory. You get a persistent, searchable memory that any MCP-compatible LLM can read and write — plus everything Obsidian gives you natively: graph view, backlinks, Bases, plugins. No sync job, no export step, no plugin to install. The same markdown files are the source of truth for both.

This is the comprehensive guide to the integration. For installation only, jump to [Quickstart](#quickstart).

---

## What this gives you

Palinode stores every memory as a plain markdown file with YAML frontmatter under `${PALINODE_DIR}`. Obsidian opens any folder of markdown as a vault. The integration is that simple — Palinode is already an Obsidian-shaped substrate. Pointing one at the other costs you a single `init` command.

Your AI agents (Claude Code, Codex, Cursor, anything speaking MCP) call `palinode_save` and `palinode_search` to read and write the vault. You open the same folder in Obsidian and see the files appear live. Edit a memory in Obsidian and the file watcher reindexes within ~2 seconds. No mode-switch, no two-source-of-truth problem.

### Where Palinode sits relative to other "LLM wiki" projects

The "LLM maintains a markdown knowledge base" pattern crystallised in 2025–2026 around [Andrej Karpathy's LLM Wiki gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f). Several community projects implement variations of it for Obsidian — Smart Connections, claude-obsidian, llm-wiki, obsidian-wiki, second-brain.

Palinode follows the **same wiki-maintenance pattern**. Wikilinks plus typed frontmatter, LLM-as-maintainer, the same `entities:` / `[[wikilinks]]` dual-surface contract. Where Palinode differs is one floor down:

- The peer projects are **patterns** — a `CLAUDE.md` and a slash-command set the LLM follows.
- Palinode is a **system** — a SQLite-vec hybrid search index, a deterministic consolidation executor, a file watcher, an MCP server, four parity surfaces (MCP / API / CLI / plugin).

You can use a peer project's pattern on top of Palinode's system — they compose cleanly because both speak markdown. The differentiator is what Palinode does behind the contract: persistent semantic search across sessions and across LLM identities, deterministic compaction operations (not "ask the LLM to merge nicely"), and the same store accessible from any MCP client without a per-tool integration.

Smart Connections, in particular, is the closest peer for the "find semantically related notes" surface. It is plugin-locked (in-Obsidian only). Palinode's equivalents (`palinode_search`, `palinode_dedup_suggest`, `palinode_orphan_repair`) are MCP-callable from any compatible client.

---

## Quickstart

The first 10 minutes. Assumes you have Palinode installed (`pip install -e .` from the repo, or whatever the install path becomes). Obsidian is a free download from [obsidian.md](https://obsidian.md).

### 1. Initialise the vault

```bash
palinode init --obsidian ~/palinode-vault
```

This creates the directory layout, a starter `_index.md` (a Map of Content linking to each category folder), a `_README.md` orientation page, and an opinionated `.obsidian/` config with sane defaults — `archive/` and `logs/` hidden, daily-notes pointed at `daily/`, graph view colour-coded by `category:` frontmatter. It also initialises a git repo and writes the first commit.

The scaffold is idempotent: re-running won't clobber files you've edited. Pass `--force-obsidian` if you want to re-apply the vault config after editing it (Obsidian's `workspace.json` is preserved either way — that's Obsidian-owned).

### 2. Open it in Obsidian

```bash
open -a Obsidian ~/palinode-vault     # macOS
obsidian ~/palinode-vault             # Linux (via the .deb / AppImage launcher)
```

If this is your first vault, Obsidian asks "Open folder as vault?" — yes. Otherwise the vault picker appears; click "Open folder as vault" and pick the directory.

You'll see the category folders (`people/`, `projects/`, `decisions/`, `insights/`, `research/`, `daily/`) plus `_index.md`, `_README.md`, and `PROGRAM.md`. `archive/` and `logs/` are filtered out of the file explorer by the scaffold's `userIgnoreFilters`.

Click `_README.md` first. One page of orientation — what each folder is for, which frontmatter fields you can edit, the difference between Palinode search and Obsidian search.

### 3. Start the API and watcher

```bash
palinode-api &        # FastAPI server on :6340
palinode-watcher &    # file indexer daemon
```

Or, if you've configured Palinode's MCP server in your IDE already, the API and watcher are likely already running as services (see [MCP-SETUP.md](MCP-SETUP.md) and [INSTALL-CLAUDE-CODE.md](INSTALL-CLAUDE-CODE.md)).

### 4. Save your first memory

From a Claude Code (or Codex, Cursor, etc.) session:

> Remember: we picked side-by-side vault as the Obsidian integration mode. Reasoning: zero conflict surface, no sync job, reversible.

Your assistant calls `palinode_save` over MCP. Within a few seconds a new file appears in the Obsidian file tree under `decisions/`. Click it — the YAML frontmatter is right there in the Properties panel; the rationale is in the body. Live, no reload.

The shell-equivalent of the same save:

```bash
palinode save --decision \
  --title "Obsidian integration MVP: side-by-side vault" \
  --rationale "Zero conflict surface, no sync job, reversible." \
  --entities "project/palinode"
```

### 5. Search via both surfaces

Run the same query two ways and compare:

- **Obsidian's built-in search** (`Cmd-Shift-F`): pure full-text. Finds exact-string matches.
- **Palinode hybrid search** (`palinode search "MVP for vault integration"` or via MCP): BM25 plus BGE-M3 vector similarity, RRF-fused. Finds the decision even though "MVP" and "obsidian integration" don't appear as a literal sequence; surfaces semantically related insights too.

Two searches, two purposes. Obsidian's search is exact; Palinode's is semantic. They live on the same files. Pick whichever fits the question.

### 6. Open the graph view

`Cmd-G` opens the graph view. Empty at first — the wiki contract (next section) populates it as the LLM saves and updates memories with `[[wikilinks]]` in note bodies. Within a session or two you'll see entities (people, projects) appear as nodes and connections form between them.

---

## The wiki contract

Palinode has two channels for cross-references between memories: the typed `entities:` list in frontmatter, and `[[wikilinks]]` in the note body. Both are first-class. Both must agree.

The `PROGRAM.md` "Wiki Maintenance" section is the canonical contract — it's what the LLM reads at session start. This section summarises it for users.

### Why both surfaces exist

- **Frontmatter `entities:`** is typed, machine-readable, and authoritative for the index. `palinode_search` ranks by semantic similarity but uses entities to find associatively-related files (the entity graph). Lint and consolidation operate over entities.
- **Body `[[wikilinks]]`** are what make the file useful when you read it directly or render it in Obsidian. The graph view, backlinks, and Obsidian's built-in search all key off body links — they don't read frontmatter for cross-references.

A frontmatter entry without a body link is invisible to a human reader of the note. A body link without a frontmatter entry is invisible to the index. Drift between the two is a bug, and the contract is what stops it.

### How the LLM keeps them in sync

When the LLM creates or updates a memory:

1. Decides what entities are referenced (explicitly named in conversation, or clearly implied).
2. Adds them to `entities:` in canonical form — lowercase, hyphens-not-spaces, `kind/slug`. Examples: `person/alice-smith`, `project/checkout-redesign`, `decision/drop-legacy-browsers`.
3. Writes the link inline in the body where the reference is load-bearing (`Met with [[Alice Smith]] about Q3 planning`), or — if no good inline spot exists — appends a `## See also` footer with slug-form links.
4. On update, scans both surfaces and resolves drift before saving.

The canonical frontmatter form is always `kind/slug` with a forward slash. The body wikilink may be the human-readable label (`[[Alice Smith]]`) or the slug (`[[alice-smith]]`) — both resolve to the same target file.

### The auto-footer

When `palinode_save` is called with `entities:` but the body has no inline links to those entities, Palinode appends a `## See also` block listing them as wikilinks. The block is delimited by an HTML-comment marker:

```markdown
<!-- palinode-auto-footer -->
## See also

- [[alice-smith]]
- [[checkout-redesign]]
```

The footer is **idempotent** — saving again replaces the block in place rather than stacking duplicates. It's also **detectable** — anything from the marker line onward is treated as auto-generated for purposes of embedding preprocessing (so dedup tools don't flag two notes as duplicates just because they link the same entities).

**Don't hand-edit the auto-footer.** It's a derived view of `entities:`; editing it manually creates drift the next save will overwrite. If you want a link in the body authoritatively, write it inline at a load-bearing spot — that's where the LLM treats body content as primary. The footer reflects whatever entities don't have an inline anchor. User-authored `## See also` blocks (without the marker) are left untouched.

### Lint catches the drift

Palinode's lint pass includes a `wiki_drift` check that warns when frontmatter and body diverge. Run `palinode lint` (or call it via MCP) to surface the deltas — entities listed in frontmatter without any body link, body wikilinks not listed in frontmatter, slug-form mismatches.

---

## The embedding tools

Palinode ships four embedding-aware tools the LLM can call during wiki maintenance. The first two are the highest-leverage ones and are what the contract recommends reaching for; the last two are P2 follow-ups. All are exposed across MCP, CLI, and the REST API — same capability surface from any client.

### `palinode_search`

The everyday recall tool. Hybrid BM25 + BGE-M3 vector search with RRF fusion. Use it before writing a new memory to find related material; use it during conversation to surface relevant context. Already in the toolkit since v0; mentioned here because the wiki contract recommends it as the default first move.

### `palinode_dedup_suggest`

Given a draft about to be saved, returns the top-K existing files whose embeddings are within a similarity threshold. The LLM uses this to answer "should I create a new file or update an existing one?" before it commits.

- **Default threshold:** ≥ 0.80 cosine to surface; ≥ 0.90 flagged as a strong duplicate (almost always means UPDATE, not ADD).
- **Tuneable:** `min_similarity` kwarg lets the LLM (or operator) loosen or tighten at call time.

### `palinode_orphan_repair`

Given a `[[wikilink]]` whose target file does not exist, returns existing files semantically near the link target text. The LLM proposes a redirect to one of those files, or creates the target with the candidate's content as scaffolding rather than starting cold.

- **Default threshold:** ≥ 0.65 cosine — more permissive than dedup because a wider candidate slate helps the LLM choose.

### `palinode_cluster_neighbors` and `palinode_topic_coverage` (P2)

Two further embedding tools are designed but not yet shipped:

- **`palinode_cluster_neighbors(file_path)`** — given a file, return semantically related files NOT currently linked to it. Surfaces missed link opportunities during periodic lint passes. Default threshold ≥ 0.70 AND not currently linked.
- **`palinode_topic_coverage(query)`** — given a topic phrase, return whether any existing wiki page already covers it (with similarity score). Used before starting a new ingestion: "is this redundant?" Default threshold ≥ 0.78.

These will be filed as their own issues once the MVP has real usage data to calibrate against. If you want them sooner, the underlying capability is the same `palinode_search` hybrid index — you can approximate both today with hand-rolled queries.

### Embedding preprocessing

One implementation detail worth knowing: before similarity comparisons, Palinode strips frontmatter, the auto-footer block, and `[[wikilink]]` decoration (`[[alice]]` → `alice`) from the embedded text. This prevents two notes from looking like duplicates simply because they link the same entities. It applies to both query and corpus sides of the dedup and orphan-repair tools, and lives in `palinode/core/embedding_preprocess.py` if you want to inspect the exact rules.

---

## Migration paths

### "I already have an Obsidian vault — can I just point Palinode at it?"

Not yet, cleanly. Palinode supports four integration modes:

1. **Cohabit** — Palinode reads/writes your existing vault. Conflict-prone; substantial work to do well.
2. **Mirror/export** — Palinode writes; a sync job copies into your vault. Two-way sync is hard; one-way is read-only-for-the-user. Strictly worse than mode 3.
3. **Side-by-side vault** — Palinode IS a vault. Open it in Obsidian alongside your other vaults. **This is what `palinode init --obsidian` does today.**
4. **Obsidian plugin** — TypeScript plugin running inside Obsidian. Roadmap; not shipped.

The shipped MVP is **mode 3**. Cohabit (mode 1) is still research-stage. The trade-off is straightforward: side-by-side gives you zero conflict surface and ships today; cohabit needs configurable category-dir mappings, frontmatter tolerance, lint opt-outs, and a "discover the user's existing convention" workflow before it is safe.

If you want to use Palinode against an existing vault today, you have two practical options: (a) run mode 3 next to your existing vault and open both in Obsidian, or (b) wait for mode 1 / file an issue with your specific vault layout so we can prioritise.

### "I have legacy Palinode memories without wiki-contract footers"

Memories written before the auto-footer shipped won't have `## See also` blocks for their `entities:`. The `palinode obsidian-sync` CLI is the backfill tool for that case: it scans every file, materialises the auto-footer where it's missing, and reconciles drift.

In the meantime, the auto-footer is applied lazily on every `palinode_save` to a given file. As the LLM updates older memories during normal session work, they self-heal.

### "I want to import from another vault"

Use `palinode import --from-vault`:

```bash
# Preview what would be imported (dry-run, no writes):
palinode import --from-vault ~/my-old-vault

# Import for real:
palinode import --from-vault ~/my-old-vault --apply

# Force everything into one category:
palinode import --from-vault ~/my-old-vault --into-category archive/ --apply

# Re-run and replace already-imported files:
palinode import --from-vault ~/my-old-vault --apply --overwrite
```

The importer:

1. Walks the source vault for `.md` files, skipping `.obsidian/`, `.trash/`, and hidden dirs.
2. Infers palinode category from PARA directory structure (`Projects/` → `projects/`, `Areas/` → `decisions/`, `Resources/` → `research/`, `Archive/` → `archive/`), daily-note filename patterns (`YYYY-MM-DD.md` → `daily/`), or frontmatter `type:` field. Unmatched files fall back to `archive/`.
3. Rewrites `[[wikilinks]]` to point at the new slugged destination names for any file included in the import. Links whose targets are not in the import set are left as-is and reported as orphans — run `palinode orphan-repair` after import to fix them.
4. Adds palinode frontmatter (`id`, `category`, `created_at`, `last_updated`, `source: "vault-import"`) without overwriting existing frontmatter fields.

Default is a **dry-run** — pass `--apply` to write. Re-running without `--overwrite` safely skips already-imported files.

---

## FAQ and known frustrations

### The graph view is empty when I first open the vault

Expected — the graph builds from `[[wikilinks]]` in note bodies, and a fresh vault has none. As the LLM saves memories under the wiki contract, the graph populates. You can prime it by saving 2–3 entities with cross-references (a person, a project, a decision that names both) and watch the connections form.

### My existing daily-notes plugin collides with Palinode's `daily/`

If you already use Obsidian's "Daily notes" core plugin pointed somewhere else, opening the Palinode vault could either (a) inherit your global config and write daily notes to the wrong folder, or (b) race with Palinode if both target `daily/`. The `palinode init --obsidian` scaffold sets the daily-notes path to `daily/` and the date format to match Palinode's, so the vault-local config is correct. If you see ambiguity, check `.obsidian/daily-notes.json` and `.obsidian/app.json` in the vault.

### A custom Obsidian plugin is editing files while the watcher is running

The watcher debounces but isn't bulletproof against high-frequency writes. If you have an Auto Save plugin or similar configured to flush on every keystroke, set its delay to >1s — that gives the watcher room to finish indexing one save before the next arrives. In practice this is rarely an issue with hand-typing; it shows up mostly with code-driven plugins.

### Frontmatter looks scary the first time

A typical Palinode file has 6–10 frontmatter fields. Some are owned by Palinode (`id`, `last_updated`); some you can edit freely (the body, `description`, `core: true` to mark a file as always-injected); some are advisory (`status`, `confidence`). The `_README.md` scaffold has a frontmatter cheat sheet. When in doubt: edit the body, leave `id` and `last_updated` alone, and lint will tell you if you've broken anything.

### Search results between Obsidian and Palinode don't agree

By design. Obsidian's search is full-text and exact; Palinode's is BM25 + vector and semantic. The same query will surface different files because they're answering different questions. If you want exact-string, use Obsidian; if you want "find me anything about this topic," use Palinode. They share the underlying files; only the index differs.

### Git auto-commits make the timeline noisy

Palinode commits on every save for audit history. Within a few sessions you'll have dozens of commits with messages like `palinode auto-save: decisions/foo.md`. This is the feature, not the bug — it's what makes `palinode_blame`, `palinode_diff`, `palinode_history`, and `palinode_rollback` work. If you want a friendlier overview, `palinode log` shows a summarised history; the raw `git log` is always there if you want every event.

### Bases requires a recent Obsidian

Bases is a core plugin in recent Obsidian (shipped 2024). Older Obsidian builds will see `_bases/` files as unrecognised. Update Obsidian, or ignore the directory. The scaffold doesn't currently generate Bases sample files by default; if you want one, the design doc has a worked example for `decisions.base`.

### "I closed Obsidian, edited a file in vim, reopened — the Properties panel is stale"

Obsidian caches frontmatter parses. `Cmd-R` reloads the vault. Not a Palinode bug, but it'll feel like one the first time it happens.

---

## Related work and further reading

### Specs

- [`PROGRAM.md`](../PROGRAM.md#wiki-maintenance) "Wiki Maintenance" section — the canonical LLM contract.
- [`docs/HOW-MEMORY-WORKS.md`](HOW-MEMORY-WORKS.md) — overall memory lifecycle (capture, recall, consolidation).
- [`docs/MCP-SETUP.md`](MCP-SETUP.md) — wiring Palinode's MCP server to Claude Code, Cursor, Zed, and other clients.

### Adjacent tools

- [Karpathy LLM Wiki gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) — the originating pattern.
- [Smart Connections](https://github.com/brianpetro/obsidian-smart-connections) — closest peer for the cluster-neighbours surface; Obsidian-plugin-locked.
- [AgriciDaniel/claude-obsidian](https://github.com/AgriciDaniel/claude-obsidian) — slash-command-based wiki maintenance pattern.
- [obsidian-vector-search](https://github.com/ashwin271/obsidian-vector-search) — Ollama-backed semantic search inside Obsidian.

### GitHub issues

Tracking issues for Obsidian workflow, markdown import, and related setup work live in the Palinode issue tracker.
