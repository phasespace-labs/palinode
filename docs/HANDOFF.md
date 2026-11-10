# Project handoff

A handoff passes selected project memory, such as decisions, the approaches you
rejected and their evidence, to a teammate or another agent's store as one JSON
file. You choose what leaves. The recipient chooses what to accept.

Both sides preview first. The sender sees exactly what would leave; the
recipient sees exactly what would be written. Neither step writes anything
until it's given the digest of the preview that was reviewed.

## Export

```bash
palinode handoff export --project harbor            # preview: writes nothing
palinode handoff export --project harbor \
    --out harbor.handoff.json --expect <digest>      # write the previewed bundle
```

The preview lists:

- every record that would leave, whether it was **selected** or carried as a
  **dependency**, and whether it is current or retired
- the frontmatter fields of each record that won't be sent
- quotes the record holds from other records (the quoted text travels even when
  the quoted record doesn't)
- text addressed to AI agents, flagged so you can see it; it isn't removed
- the records left out, and why
- links a recipient won't be able to follow
- a **digest** of all of the above

`--expect <digest>` writes the bundle only if the selection still matches what
you previewed. If anything changed in between, the write is refused and you
preview again. `--out` never overwrites an existing file.

### What is selected

By default, the project's current `Decision` and `Insight` records: those whose
`entities` name `project/<slug>` (or one of its [aliases](ENTITY-ALIASES.md)).
`--type` changes the types, and `--since` keeps only recently updated records.
`--path` replaces the default selection with exactly the records you name.

### What is left out

| Record | Default selection | Named with `--path` / `--include` |
|---|---|---|
| `visibility: private` or `restricted` | left out | **refused** |
| Non-memory directories, `-history.md` siblings | left out | **refused** |
| `people/`, `daily/`, `inbox/` | left out | carried, with a warning |
| `core: true` | left out | carried, with a warning |
| Retired (archived, superseded, retracted, expired, under `archive/`) | left out | carried as retired |
| Another project's record | not listed | carried, with a warning |

### Dependencies

Each selected record's `supersedes`, `superseded_by`, `backed_by`,
`contradicts`, `falsified_by`, `sources` and `claims` links are followed one
hop. A linked record is carried when it is in the same project and passes the
rules above, or when you name it with `--include`. A retired dependency travels
retired, so the recipient sees the history without it becoming current again.
Anything else stays home and is listed. Its path still appears in the link
field of the record that cites it, and the preview warns when that path belongs
to a private record. Wikilinks are navigation, not evidence, and are never
followed.

### Fields that never travel

Only identity, content, lifecycle and evidence fields are sent: `id`,
`category`, `type`, `title`, `description`, `entities`, the dates, `status`,
`lifecycle`, `expires_at`, `epistemic`, `confidence`, `sources`, `claims`,
`backing_policy`, `auditable_memory`, and the link fields above. Everything
else, including `core`, `authority`, `update_policy`, `priority`, `scope`,
`visibility`, `access` and `external_refs`, is dropped and named in the
preview. A bundle can't make a record core, change how it may be updated, or
widen who sees it. Triggers live only in the database and are never part of a
bundle.

### What the bundle says about what it left out

The bundle's manifest counts the left-out records by reason. It doesn't name
them, because a record's path can itself be private. The full list stays in
your preview.

## Import

```bash
palinode handoff import harbor.handoff.json --project harbor   # preview: writes nothing
palinode handoff import harbor.handoff.json --project harbor \
    --apply --expect <digest>                                    # write the previewed records
```

The preview shows, for each record in the bundle:

- **`+` new**: it lands at the same path it had in the sender's store
- **`+` renamed**: that path is already taken in your store, so it lands as
  `<slug>-handoff-<id>.md`, and links to it from the rest of the bundle are
  rewritten to follow it
- **`=` already here**: this record, with this exact content, came in with an
  earlier import (from any bundle) or is already at that path
- **`x` rejected**, and why

It also lists the frontmatter a bundle carried that is ignored, text addressed
to AI agents, how many records the sender left out and why, links that point at
nothing in your store, and a **digest**. `--apply --expect <digest>` writes the
records only if neither the bundle nor your store changed since the preview.

### What an import can't do

- **It can't overwrite or edit anything you have.** Every record lands in a new
  file. Your existing records aren't touched, not even to add a reciprocal link.
- **It can't give a record authority.** Only the fields an export sends are read
  (see [Fields that never travel](#fields-that-never-travel)). Anything else a
  bundle carries, such as `core`, `authority`, `update_policy`, `scope`,
  `visibility` or `source`, is ignored and named in the preview. A bundle is a
  file anyone can edit, so this is checked on import, whatever the sender's
  side claims.
- **It can't pass for your own notes.** Every imported record says
  `source: handoff-import` and carries an `imported_from` block: the bundle id,
  the path and content hash it had in the sender's store, when it was exported
  and when you imported it.

### What is rejected

A record is rejected, and the rest of the bundle still imports, when:

- its path isn't a plain `category/slug.md` inside the store: no `..`, absolute
  paths, dot-directories, non-memory directories, `-history.md` siblings, or a
  directory that resolves outside the store
- its body doesn't match its `content_hash` (changed after export)
- its body is over 512 KiB, or the bundle holds more than 2,000 records (the
  API also refuses requests over 5 MB)
- its text fails the same security scan as a save, or carries tool-call markup
- a link, source or claim field is malformed

Text addressed to AI agents ("note to any agent…") is flagged in the preview
and imported. Like any memory, it's withheld when it's delivered to an agent.

### Which project the records belong to

The records are filed under `--project`. Their `project/<sender's slug>` entity
is rewritten to `project/<yours>`, and other entities are kept. Without
`--project`, the import uses the sender's project, and refuses if your store
doesn't have it.

### If an import is interrupted

The records are written one file at a time and committed together in one
commit, whose message names the bundle (`Palinode-Handoff-Bundle: hb-…`). If
the commit fails, the files stay on disk and the result says so. Preview the
same bundle again: those records show as already here but uncommitted, and
applying commits them.

## A bundle is an independent copy

Once the file leaves your machine it is a copy you no longer control.
Correcting, archiving or deleting a record in your store later doesn't change
or erase the bundle, or anything imported from it. To pass on a correction,
export again. See [DATA-LIFECYCLE.md](DATA-LIFECYCLE.md) for the copies that
retirement and erasure don't reach.

## Surfaces

CLI and REST only: `POST /handoff/export`, `POST /handoff/import` (preview) and
`POST /handoff/import/apply`. No MCP tool is exposed: an export or import goes
through the CLI or REST preview-and-digest workflow, never an agent's tool call. A store-wide capture pause
(`palinode controls`) stops an import apply, and doesn't stop a preview.
