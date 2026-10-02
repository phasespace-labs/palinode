# Entity aliases

A subject written several ways becomes several entities. `project/orbit-app`,
`project/orbitapp` and `project/Orbit_App` are three nodes in the entity graph
unless you say they are one. Every lookup on one spelling then returns a
plausible, incomplete answer, and nothing in the answer says so.

`entity-aliases.yaml` is where you say it. It is a small, human-curated file
of groups. Palinode reads it at query time; it never rewrites a memory to
match it.

Since project isolation, the file does more than widen entity lookup: it
decides which records count as a project's own. A missing group can silently
withhold a project's memories from recall. Curate it with the same care as
`palinode.config.yaml`.

## The file

```yaml
aliases:
  project/orbit-app:
    - project/Orbit_App
    - project/orbitapp
  person/ada-lovelace:
    - person/ada
```

- Each key is the **canonical ref** of a group. The list holds the **other
  spellings** of the same subject.
- A ref is `category/name`, the same shape as an `entities:` entry.
- A group of one (a key with an empty list) aliases nothing and is ignored.
- A ref belongs to one group. If a hand edit puts it in two, the reader merges
  the two groups and logs a warning; `palinode aliases add` refuses to create
  that state.

### Where it lives

`<PALINODE_DIR>/entity-aliases.yaml`, at the root of the memory store, beside
the category folders. It is git-versioned **in the store**, with the memories
it describes, so `git log entity-aliases.yaml` in the store is its history.

It never belongs in a code repository. A real store's file names real people
and projects by their actual refs, which makes it one of the most identifying
files you have. Keep it out of anything you publish.

## How it is used

**At query time, never on disk.** An entity lookup on any member returns the
union of the whole group: asking for `person/ada`, `person/ada-lovelace` or
either spelling gives the same files. Stored frontmatter keeps its original
refs. Removing a line undoes a wrong group completely, which is why curation
is safe to try: a wrong merge made by rewriting files could not be taken back.

**The canonical ref names the group.** Where Palinode has to report one name
for a group, such as the project a request resolved to, it reports the key as
the file spells it. A request resolved to `project/orbitapp` is reported as
`project/orbit-app`.

**Project refs compare case-insensitively.** For project scope and isolation,
`project/Orbit_App` and `project/orbit_app` are one project even without a
group. Entity lookup (search's `entities` filter, `palinode entities`) is
exact, so a case variant that should show up there still needs a line.

## Curating it

Use `palinode aliases` (see [CLI.md](CLI.md#palinode-aliases)) rather than
editing the YAML by hand. Every change goes through the API, is written sorted
and committed in the store's git.

```bash
palinode aliases check                          # what needs a decision
palinode aliases add project/orbit-app project/orbitapp project/Orbit_App --dry-run
palinode aliases add project/orbit-app project/orbitapp project/Orbit_App
palinode aliases list                           # groups, with file counts
palinode aliases remove project/Orbit_App
```

- `add` applies by default and `--dry-run` shows the diff without writing.
- A ref already in another group is refused unless you pass `--move`. With
  it, the ref leaves its old group, and a group left empty is removed.
- A ref that is another group's canonical is always refused. Regroup its
  members first.
- `remove` takes a member. To delete a group, remove its members; the last
  removal removes the group.
- The command rewrites the whole file in a fixed order, so **comments in a
  hand-edited file are not kept**. A malformed file is refused rather than
  rewritten; fix it by hand first.

### Which spellings to merge is a judgement call

`palinode lint` and `palinode aliases check` report candidates. They never
merge, and neither should a script. Two kinds of candidate need different
answers:

- **Separator variants** (`orbit-app`, `orbitapp`, `Orbit_App`) are almost
  always one subject. Merge them.
- **Prefix clusters** (`harbor`, `harbor-dev`, `harbor-docs`) might be one
  project spelled several ways, or a family of real, separate projects. Decide
  each pair on what the records are about. Keep pairs **deliberately split**
  when isolation between them is the point, such as a public product
  (`project/harbor`) and the private development project behind it
  (`project/harbor-dev`). Merging those would deliver the private project's
  decisions into sessions scoped to the public one.

A wrong merge costs less than you might fear (delete the line), but while it
stands it widens what every scoped session sees.

## Interaction with `context.project_map`

`context.project_map` in `palinode.config.yaml` and the alias file answer
different questions:

| | `context.project_map` | `entity-aliases.yaml` |
|---|---|---|
| Maps | a directory or repository name → a project ref | ref spellings → one group |
| Matching | exact and case-sensitive on the directory name | case-insensitive for `project/` refs |
| Lives in | the server's config file | the memory store, git-versioned |
| Used for | resolving *which* project a request is in | deciding which records *belong* to it |

They compose. A request from `~/src/orbit` with
`project_map: {orbit: project/orbitapp}` resolves to `project/orbitapp`; the
alias group above then makes that `project/orbit-app`, and every record tagged
with any member counts as its own.

## Interaction with recall isolation

A request whose project resolves leaves out records tagged only to other
projects (see [HOW-MEMORY-WORKS.md](HOW-MEMORY-WORKS.md) and
[HARNESSES.md](HARNESSES.md#project-scope-when-the-server-is-on-another-machine)).
Both sides compare through the alias groups: a record tagged
`project/orbitapp` is `project/orbit-app`'s own, and so is a request resolved
to any member.

So an unaliased spelling is **another project** as far as isolation is
concerned. Its records are withheld from sessions in the project they belong
to, and counted as `other_projects_withheld`.

## The doctor check

`palinode doctor` runs `project_tags_unmapped`: it lists `project/*` tags on 10
or more files that neither a group in this file nor a `project_map` target
covers. It is a warning, not an error, because an unmapped tag may be a real,
separate project. `palinode aliases check` runs the same check next to the
alias lint.

For each tag it names, decide: add it to a group (`palinode aliases add`), map
the repository with `project_map`, or leave it alone as the separate project
it is.
