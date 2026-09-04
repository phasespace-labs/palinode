---
created: 2026-09-04T00:00:00Z
category: documentation
---

# Local provenance UI

Palinode includes a local, server-rendered inspector for browsing what the
agent remembers and the evidence available for each memory. It runs inside the
existing API process at `/ui`; there is no separate UI service, JavaScript
build, CDN, or account to configure.

The inspector is designed for local audit work. It shows memory health,
browsable files, search results, recent Git changes, consolidation history,
quality queues, and a provenance panel for each memory.

## Open the UI

Start Palinode in the foreground:

```bash
palinode start
```

Keep that terminal open, then visit:

<http://127.0.0.1:6340/ui/>

`palinode start` launches both the API and file watcher. If you already run the
systemd services, Docker Compose stack, or `palinode-api` directly, do not
start a second copy: open the same URL after confirming the API is healthy.

```bash
curl http://127.0.0.1:6340/health
```

The URL follows the configured API port. For example:

```bash
PALINODE_API_PORT=7000 palinode start
```

opens the inspector at `http://127.0.0.1:7000/ui/`.

### Inspect a remote machine safely

The API process that serves the UI must still bind to loopback on the remote
machine. Forward that loopback port over SSH:

```bash
ssh -L 6340:127.0.0.1:6340 user@memory-host
```

Then open `http://127.0.0.1:6340/ui/` in your local browser. This does not make
the UI listen on the remote machine's LAN interface.

## Views

| View | Path | What it answers |
|---|---|---|
| Dashboard | `/ui/` | How many browsable memories and indexed chunks exist? Which health counts need attention? What changed recently? |
| Memory | `/ui/memory` | Which memory files can I browse? Which are core, fresh, aging, stale, or a given type? |
| Search | `/ui/memory?q=terms` | Which indexed memories match this query? |
| Fact detail | `/ui/memory/<category>/<slug>` | What does this memory say, what metadata does it carry, and what provenance is available? |
| Diffs | `/ui/diffs` | Which memory files changed in recent Git commits? |
| Compaction | `/ui/compaction` | Which consolidation passes ran, and which archived-fact history files exist? |
| Quality | `/ui/quality` | Which memories are stale, orphaned, missing descriptions, contradictory, or missing extraction metadata? |

### Dashboard

The dashboard combines file, index, lint, and Git information:

- **Memories** counts browsable Markdown files on disk.
- **Chunks** counts records in the derived SQLite index.
- **Core**, **stale**, **orphaned**, **no description**, and
  **contradictions** come from the same lint data used by `palinode lint`.
- **Commits / 7d** counts recent commits in the memory repository.
- **Recent memory** comes from the index and is deduplicated by file.

If files exist but the index has no chunks, the page says so explicitly. Start
the watcher or run `palinode reindex`; file-based counts and browsing still
work while search and index-backed recent memory remain unavailable.

### Browse and search

The unfiltered Memory view reads the Markdown files, because files are
Palinode's source of truth. Its filters include:

- `core`
- memory type
- `fresh` (updated within 7 days)
- `aging` (8–90 days)
- `stale` (more than 90 days)

The browsable list omits operational or special-purpose paths: `daily/`,
`archive/`, `inbox/`, `logs/`, `prompts/`, `.obsidian/`, and consolidation
siblings ending in `-history.md`. History files remain available from the
Compaction view.

Search is different from browsing: it uses the existing hybrid search path and
therefore needs an index and reachable embedding backend. If that backend is
unavailable, the page keeps working and displays a `search unavailable`
notice instead of failing the whole view.

### Fact detail and provenance

Selecting a memory renders its Markdown body and useful frontmatter fields,
including type, ID, confidence, priority, status, and recorded recall count
when present. Raw HTML is disabled during Markdown rendering, and the result
is sanitized before it reaches the page.

The provenance panel distinguishes information Palinode has from information
it does not yet capture. Depending on the file, real rows can include:

- the source file path;
- an explicit epistemic claim type such as `fact`, `inference`,
  `open_question`, or `unverified`;
- the latest Git commit that saved the file;
- a `supersedes` target; and
- recall count and last-recalled date.

Rows marked `G1`, `G2`, `G3`, `G4`, `R1`, or `R2` are explicit provenance
gaps, not successful attestations. For example, extraction identity, source
span, and a trusted timestamp may say “not captured.” An absent epistemic field
is shown as **unmarked**, never silently promoted to **fact**.

The current detail route does not yet run a content-hash mismatch check. The
visual “chain intact” state is therefore not an independent cryptographic
attestation; use the underlying Git history and Palinode's validation tools
when investigating integrity.

### Diffs and compaction

Diffs groups recent commits by day and shows only touched Markdown memory
files. Database, journal, and log files are deliberately removed from this
view. The default window is 14 days; choose another window from 1 to 365 days
with a query parameter, for example `/ui/diffs?days=30`.

Compaction reads commits whose subjects identify a compaction or nightly pass,
plus the `-history.md` audit-trail files on disk. Its default window is 90 days
and accepts the same 1–365 day range, for example
`/ui/compaction?days=180`. Opening this view never starts consolidation.

### Quality queues

Quality presents the current lint findings as linkable queues:

- stale active memories;
- orphaned memories with no entity relationship or inbound reference;
- memories missing a one-line description;
- contradictory active memories; and
- memories whose extraction provenance is not yet captured.

The sidebar badge counts the actionable stale, orphaned, missing-description,
and contradiction queues. It intentionally excludes the extraction-metadata
queue while that metadata is not captured for ordinary memories.

## Read-only and access boundaries

Every UI route is a `GET` route. The inspector offers no save, edit, archive,
rollback, reindex, or consolidation action, and browsing it does not modify or
commit memory Markdown.

Search has one narrower operational side effect: because the UI deliberately
reuses Palinode's normal search capability, a successful query updates recall
metadata in the derived SQLite index and records normal retrieval telemetry.
It still does not change the source Markdown or create a Git commit. Merely
opening the dashboard, lists, diffs, compaction, quality, or fact page does not
perform that search accounting.

### Loopback is mandatory

The UI refuses to render when `PALINODE_API_HOST` resolves to a non-loopback
address. A public bind such as `0.0.0.0` receives HTTP 403 for `/ui`, even when
either of these API options is set:

- `PALINODE_API_BIND_INTENT=public`
- `PALINODE_API_ALLOW_UNAUTH=1`

Those options affect API deployment; they do not override the inspector's
separate loopback guard. Keep `PALINODE_API_HOST=127.0.0.1` (the default), use
`localhost`/`::1`, or use the SSH-forwarding pattern above.

If `PALINODE_API_TOKEN` or `PALINODE_API_TOKEN_FILE` is configured, the API's
bearer middleware also protects `/ui` and its static assets. The UI has no
sign-in form. A client must add `Authorization: Bearer <token>` to every UI
request; do not put the token in the URL.

Anyone who can open the UI can read the memory content it displays. The
loopback refusal is therefore a security boundary, not a deployment
suggestion. See [SECURITY.md](../SECURITY.md#api-authentication) for the API's
separate authentication rules.

## Data sources and graceful degradation

| Source | Used for | If unavailable |
|---|---|---|
| Markdown files | memory count, browse list, fact body and frontmatter | There is no memory content to display. |
| SQLite index | chunk count, search, recent memory, recall statistics | File browsing still works; search/recent/index metrics are empty or degraded. |
| Git repository | recent changes, compaction commits, saved lineage | Those sections show no history; memory content still renders. |
| Lint pass | health cards and quality queues | The affected request cannot build its health context. |
| Embedding backend | semantic/hybrid search queries | Search shows a soft unavailable notice; other views remain usable. |

## Installation footprint

The inspector ships as part of the standard Palinode installation, not an
optional extra. Three required runtime dependencies exist specifically for
this surface:

- `jinja2` renders the server-side templates;
- `markdown-it-py` renders memory Markdown with raw HTML disabled; and
- `nh3` sanitizes the resulting HTML as a defense-in-depth boundary.

`nh3` is also the heaviest single import in the current CLI startup profile.
There is no supported “headless without UI dependencies” installation profile
today.

## Troubleshooting

| Symptom | Check |
|---|---|
| Browser cannot connect | Confirm `palinode-api` is running and `curl http://127.0.0.1:6340/health` succeeds. |
| HTTP 403 with “loopback-only” | The API is configured with a non-loopback host. Restart it with `PALINODE_API_HOST=127.0.0.1`; public-intent and unauthenticated opt-out flags do not override this guard. |
| HTTP 401 | Bearer authentication is enabled. The UI has no login page; ensure the browser client supplies the header on HTML and static-asset requests. |
| Memories exist but chunks/search/recent are empty | Start the watcher or run `palinode reindex`, then refresh. |
| Search alone says unavailable | Check the embedding service with `palinode doctor`. File and Git views do not require embeddings. |
| Diffs or saved lineage are empty | Confirm the memory directory is a Git repository with commits in the selected time window. |
| Compaction is empty | No matching compaction/nightly commit exists in the selected window; viewing the page does not run one. |

For service and recovery procedures, see the
[Operations guide](OPERATIONS.md). For the relationship between files, the
index, Git, and recall, see [How memory works](HOW-MEMORY-WORKS.md).
