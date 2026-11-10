---
created: 2026-03-22T20:45:00Z
category: documentation
---

# Palinode Quickstart

This is the canonical first-use path: install, start, inspect controls, save a
decision, connect a client, open a fresh session, inspect its source, then
correct it. Your **memory store** is a private Git-backed markdown directory;
it is not the Palinode code checkout.

## Before you start

The model-free path below is available in **v0.21.0**. It uses explicit lexical
retrieval and client-native `mcp-config` output. The commands pin that release
tag; a newer [release](https://github.com/phasespace-labs/palinode/releases/latest)
or [Homebrew install](HOMEBREW.md) can be used after checking its documented
retrieval requirements.

For v0.21, you need Python 3.11+ and Git. These are POSIX-shell instructions,
tested on macOS; use the release's platform-specific installation guidance on
other platforms. Lexical mode does not need Ollama, an embedding endpoint, or a
chat model. Hybrid mode needs an embedding endpoint for indexing and search;
chat remains optional and is only used for features such as consolidation.
Setting `PALINODE_RETRIEVAL_MODE=lexical` is an explicit mode choice, not a
silent fallback when hybrid's endpoint is unavailable. See
[PRIVACY.md](PRIVACY.md) before choosing a remote endpoint.

## 1. Install the v0.21.0 release checkout

Keep the code checkout separate from your memory store. Use the same checkout
path in terminal two.

**Before your first capture:** Palinode stores readable Markdown and Git history. `private`/`restricted` control discovery by scope; a caller with API access can still read a hidden memory by its known path and use full-store maintenance tools. These labels provide no encryption or per-user/per-agent authentication. Protect the store, backups and API credentials; use separate instances or filesystem permissions for stronger separation. See the [privacy contract](PRIVACY.md).

```bash
git clone https://github.com/phasespace-labs/palinode.git "$HOME/palinode-src"
cd "$HOME/palinode-src"
git checkout v0.21.0
python3 -m venv .venv
. .venv/bin/activate
pip install -e .

export PALINODE_HOME="$(pwd)"
export PALINODE_BIN="$PALINODE_HOME/.venv/bin"
export PALINODE_DIR="$HOME/.palinode"
mkdir -p "$PALINODE_DIR"
git -C "$PALINODE_DIR" init
git -C "$PALINODE_DIR" config user.name "Your Name"
git -C "$PALINODE_DIR" config user.email "you@example.invalid"
```

`PALINODE_DIR` contains your memories, index, logs, and Git history. Keep it
private; do not put it inside the checkout or publish it. A remote Git push is
optional and is your responsibility to configure.

## 2. Start Palinode in terminal one

`palinode start` keeps the API and watcher in the foreground. Leave this
terminal open. The explicit retrieval mode is an environment setting, so it
must be present on the service process and in each client terminal.

```bash
# Keep the PALINODE_HOME and PALINODE_BIN values established in step 1.
export PALINODE_DIR="$HOME/.palinode"
export PALINODE_RETRIEVAL_MODE=lexical
"$PALINODE_BIN/palinode" start
```

While it runs, the same process serves a read-only memory inspector at
<http://127.0.0.1:6340/ui/>. Step 4 opens your first saved memory there.

## 3. Before enabling automatic capture: inspect and control it

Open terminal two while the API continues running in terminal one. Set its
environment before using controls; a new shell does not inherit terminal
one's variables. Inspect the result before `palinode init --hook` or asking
a client to save automatically.

```bash
# Use the same absolute checkout path chosen in step 1.
export PALINODE_HOME="$HOME/palinode-src"
export PALINODE_BIN="$PALINODE_HOME/.venv/bin"
export PALINODE_DIR="$HOME/.palinode"
export PALINODE_RETRIEVAL_MODE=lexical
"$PALINODE_BIN/palinode" controls status --format json
```

It reports the effective store and policy-resolved project, configured
destinations and Git remotes (with URL credentials and query values redacted),
and files it can observe in this project. A generated hook/config file is not
proof that a client process is running; Palinode cannot inspect client-managed
model traffic. Local storage is also not a no-network promise when an embedding
provider, consolidation provider, Git remote, or client provider is configured.

For a usable **recall-only** session on this Palinode instance, pause future
API capture while leaving recall active. This is useful when an agent should
search existing context but must not write new memories:

```bash
"$PALINODE_BIN/palinode" controls pause --capture --no-recall
"$PALINODE_BIN/palinode" controls status
# Review http://127.0.0.1:6340/ui/ and `palinode history <memory-file>`.
"$PALINODE_BIN/palinode" search "SQLite local prototype"
"$PALINODE_BIN/palinode" save --type Decision \
  "Fictional refused capture while controls are paused."
"$PALINODE_BIN/palinode" controls resume --capture --no-recall
```

The text status after the pause begins with `Capture: paused` and `Recall:
active`; the attempted save is refused by the server before it stores the
fictional content, while search remains available. On a newly created empty
store, that search can return no results; it demonstrates recall availability,
not that an earlier decision was found. `resume --capture
--no-recall` restores capture without changing the recall setting. This is an
instance-wide API control: it affects every future API-routed capture request
for this Palinode store, not only one agent or chat.

Pause applies only to future capture and recall requests routed through this
API. It does not stop background indexing, enrichment, or consolidation;
direct-file legacy clients; in-flight requests; context already delivered to a
client; or client-native memory and model traffic. To omit a project or source
path from **automatic** capture and recall, use bounded exclusions:

```bash
"$PALINODE_BIN/palinode" controls exclude-project private-prototype
"$PALINODE_BIN/palinode" controls exclude-path "$PWD/.env"
```

Project exclusions also stop automatic enrichment (descriptions and summaries)
and consolidation of that project’s stored notes. Project membership uses the
note’s `project/<slug>` entities and the same project aliases as recall; daily
consolidation also uses its existing project-reference grouping. Notes remain
stored, indexed, and searchable by explicit recall. Path exclusions apply to
capture sources only (transcript source paths and cwd), not memory-file paths;
they do not stop enrichment or consolidation of stored notes.

Exclusions do not block an explicit save, explicit recall, or other explicit
user/API input, and they are not a secret-scanner guarantee. Do not submit
secrets as explicit content. Transcript capture remains an opt-in harness
feature; MCP installation alone does not enable it, and the command cannot
certify a client process is running. See [HARNESSES.md](HARNESSES.md) for source/range and
automatic-versus-explicit behavior, and [PRIVACY.md](PRIVACY.md) for visibility
and access limits.

### Switch the hooks off

Pausing is reversible without changing hook registrations. To pause both future
API-routed capture and recall, then inspect the policy and observed hooks:

```bash
"$PALINODE_BIN/palinode" controls pause
"$PALINODE_BIN/palinode" controls status
```

Use `controls pause --no-recall` for capture only; `controls resume` restores
both. A pause leaves the hooks installed and does not stop background indexing,
enrichment, or consolidation. Project exclusions have the additional stored-note
processing scope described above.

`palinode init --hook` opts in to four Claude Code hooks:

| Event | Script | What it does |
|---|---|---|
| `SessionStart` | `palinode-session-start.sh` | Primes a session with memory context. |
| `UserPromptSubmit` | `palinode-user-prompt-submit.sh` | Recalls relevant memory before a prompt. |
| `SessionEnd` | `palinode-session-end.sh` | Submits a floor summary: message count, a first-prompt topic hint bounded to 200 characters, and the last assistant reply capped at 2000 characters by default (`PALINODE_HOOK_ENDING_MAX_CHARS`; `0` disables the ending). |
| `PostCompact` | `palinode-post-compact.sh` | Saves Claude Code’s compaction summary as a session checkpoint. |

To remove these hooks permanently, inspect all three registration locations:
user `~/.claude/settings.json`, project `.claude/settings.json`, and project-local
`.claude/settings.local.json`. `palinode controls status` lists the observed
Palinode hooks with their settings scope; it does not certify a running client.
In each applicable JSON file, remove the Palinode command object from the
corresponding event’s `hooks` array. Remove an enclosing entry/event only if it
is then empty; keep other hooks and settings intact. A registration in another
scope can still run the hook.

After removing every registration for a script, delete the script at the path
its command named, typically `.claude/hooks/<script>` or
`~/.claude/hooks/<script>`. For example, after removing the project’s SessionEnd
registration (and checking that no other registration uses that copy):

```bash
rm .claude/hooks/palinode-session-end.sh
```

Repeat for whichever of the four scripts you are removing, then run
`palinode controls status` again and start a new Claude Code session. Removing
hooks deletes no memories, index, or Git history. MCP tools and instruction-driven
explicit saves can still be used. `palinode init --no-hook` skips hook
installation; it does not uninstall existing registrations or scripts.

For Codex, `palinode init --codex-hook` opts in to the `SessionStart`,
`UserPromptSubmit` and `SessionEnd` scripts, registered in `.codex/hooks.json`.
Codex runs them after you trust them with `/hooks`. To remove them, delete the
three palinode entries from `.codex/hooks.json` (leave other tools' entries in
place) and the scripts under `.codex/hooks/`. If you registered them yourself in
the user-level `~/.codex/hooks.json`, remove them there too; `controls status`
lists Codex hooks from both files. See
[examples/hooks/](../examples/hooks/README.md#codex).

For Cursor, `palinode init --cursor-hooks` registers `sessionStart` and
`beforeSubmitPrompt` in `.cursor/hooks.json`. For Antigravity, `--agy-hooks`
registers `SessionStart` and `PreInvocation` under the `palinode` hook in
`.agents/hooks.json`. Both are recall only: they inject memory and capture
nothing. To remove them, delete the palinode entries from that `hooks.json`
(keep other entries) and the scripts under `.cursor/hooks/` or `.agents/hooks/`.
`controls status` lists them as recall-only hooks.

Pause or remove? `controls pause` acts on the server, for every client that
routes through this API, and `controls resume` undoes it; the hooks still run
and are refused. Removing a hook acts on one client and one scope, and stays
until you reinstall it. Neither recalls context already delivered: memory a hook
injected into a running session stays in that client's conversation until the
session ends, and anything the client sent to its own model provider is outside
Palinode's control.

## 4. Verify and save a decision in terminal two

Continue in terminal two with the environment from step 3.

```bash
"$PALINODE_BIN/palinode" doctor
"$PALINODE_BIN/palinode" status --format json
"$PALINODE_BIN/palinode" save --type Decision \
  --project harbor-notes --slug harbor-notes-storage \
  "Use SQLite for the local prototype because it runs as a single-user desktop app."
"$PALINODE_BIN/palinode" search "SQLite local prototype"
```

Expected results: `save` reports `retrieval_mode: lexical`, `indexed: true`,
and `git_committed: true`; the search returns `decisions/harbor-notes-storage`.
If it does not, run `doctor` first. Do not infer a semantic-match quality claim
from lexical mode: it is exact-term retrieval for a supported minimal path.

### Open your memory

With terminal one still running, open the decision you just saved in a browser:

<http://127.0.0.1:6340/ui/memory/decisions/harbor-notes-storage>

(`open` on macOS, `xdg-open` on many Linux desktops, or paste the URL on
Windows.) The page shows the saved text, its metadata, and a provenance panel
with the source file and the Git commit that saved it. The dashboard at
<http://127.0.0.1:6340/ui/> links to the Memory, Diffs, Compaction and Quality
views. If you changed `PALINODE_API_PORT`, use that port instead of `6340`.

The inspector is **read-only**: browsing it never edits, archives or commits a
memory. Its *Correct or retire this* section lists commands to run elsewhere;
step 6 does exactly that. It serves only on loopback and has no sign-in form,
so on a token-enabled server the browser must already send the bearer token
with every request. For a store on another machine, use the SSH-forwarding
pattern and the access limits in [UI.md](UI.md).

## 5. Connect a client, then prove a fresh session

Generate the native fragment for the client instead of copying a command path.
For example, v0.21.0 emits a TOML fragment for Codex:

```bash
"$PALINODE_BIN/palinode" mcp-config --editor codex --stdio --project harbor-notes
```

The explicit project is emitted as `PALINODE_PROJECT` for this stdio server
process, so later searches and fresh sessions keep `harbor-notes` even from a
linked worktree. A `palinode_session_init` project argument scopes that call;
it does not change the scope of later calls. For a project-local `.mcp.json`,
`palinode init --pin-project` writes the same setting. For a different project,
generate a separate client entry. For a remote HTTP server, `--http --project
harbor-notes` emits the same scope as an `X-Palinode-Project` request header:
the server cannot see the client's directory, and without the header a remote
call is unscoped rather than scoped to the server's own checkout.

Every scoped response then names the project and the source that decided it —
search leads with `Scope: project/harbor-notes (environment)` and the
session-start digest heads with `Session context: project/harbor-notes
(environment; known)`.

Merge the result into the destination named in its instructions, then restart
the client. Use [MCP install recipes](MCP-INSTALL-RECIPES.md) for the selected
client and [MCP configuration homes](MCP-CONFIG-HOMES.md) to diagnose a config;
the command never edits client configuration itself.

Start a genuinely new chat/session and ask the connected client to call
`palinode_session_init` for `harbor-notes`. For a deterministic CLI check of
the same session-start digest, run:

```bash
"$PALINODE_BIN/palinode" prime --project harbor-notes
```

It should report `project/harbor-notes` resolved explicitly and list the recent
storage decision. MCP-only clients require this explicit tool call. Automatic
session-start injection is only available when the selected client has its
documented integration/hooks; it is not implied merely by installing MCP. See
[HARNESSES.md](HARNESSES.md) for the supported harness tiers, hooks, and
per-client behavior.

## 6. Inspect, then correct the decision

Before changing anything, inspect the exact markdown and provenance:

```bash
"$PALINODE_BIN/palinode" read decisions/harbor-notes-storage.md --meta
```

Or reopen it in the inspector from step 4. The inspector is read-only and
loopback-only, and loopback is not an authorization boundary. See
[UI.md](UI.md) for its visibility and deployment limits.

First save a neighboring decision that must remain unchanged, then save the
separate requirement that supports the hosted-service decision. Record the
shared-service decision under a **new** slug with a typed supersession link,
a typed support link to that requirement, and a historical quote from the
initial SQLite decision. Finally archive the initial decision with the
replacement reference and a reason. This preserves the original markdown and
Git history while removing it from default recall.

```bash
"$PALINODE_BIN/palinode" save --type Decision \
  --project harbor-notes --slug harbor-notes-timestamps \
  "Store event timestamps in UTC."
"$PALINODE_BIN/palinode" save --type Decision \
  --project harbor-notes --slug harbor-notes-concurrent-write-requirement \
  "The shared hosted service requires transactional coordination for concurrent writers."
"$PALINODE_BIN/palinode" save --type Decision \
  --project harbor-notes --slug harbor-notes-storage-shared \
  --cite "decisions/harbor-notes-storage.md::Use SQLite for the local prototype because it runs as a single-user desktop app." \
  --cite "decisions/harbor-notes-concurrent-write-requirement.md::The shared hosted service requires transactional coordination for concurrent writers." \
  --backed-by decisions/harbor-notes-concurrent-write-requirement \
  --metadata-json '{"supersedes":"decisions/harbor-notes-storage.md"}' \
  "Use PostgreSQL for the shared hosted service because concurrent writers need transactional coordination."
"$PALINODE_BIN/palinode" archive decisions/harbor-notes-storage.md \
  --superseded-by decisions/harbor-notes-storage-shared.md \
  --reason "The shared hosted service needs concurrent-write coordination."
```

Start a fresh session again and ask it to search for the shared hosted-service
decision, then inspect its source and the unaffected UTC neighbor:

```bash
"$PALINODE_BIN/palinode" prime --project harbor-notes
"$PALINODE_BIN/palinode" search "shared hosted service"
"$PALINODE_BIN/palinode" read decisions/harbor-notes-storage-shared.md --meta
"$PALINODE_BIN/palinode" read decisions/harbor-notes-timestamps.md --meta
```

The search finds the new shared-service decision; its metadata includes the
supersession, a typed support link to the concurrent-writers requirement, and
the preserved SQLite history reference. The initial SQLite decision is
archived, not replaced in place, and the UTC neighbor remains a separate
unchanged file.

The SQLite quote is historical lineage: it proves that the quoted text was
found in that saved record when cited. It does not support the PostgreSQL
rationale, and quote integrity does not establish that a quoted claim is true.
The separately saved concurrent-writers requirement is the recorded support
for the new rationale; assess that requirement's source and claim status on
its own merits.

The two steps above — save the replacement, then archive the original with
`--superseded-by` — are also available as one reviewed operation that previews
first, checks that the target has not changed since you looked, and prints the
recovery command: `palinode corrections preview` / `apply`. See
[CORRECTIONS.md](CORRECTIONS.md) for that walkthrough, what gets refused and
why, and the undo path.

### Correct or retire through your agent

A connected client can do the same thing with Palinode's tools. Ask it, for
example: *"Archive decisions/harbor-notes-storage.md, superseded by
decisions/harbor-notes-storage-shared.md, because the shared hosted service needs
concurrent-write coordination."* It calls `palinode_archive` with those
arguments. Check the result with `read --meta` or in the inspector rather than
taking the reply on trust.

To retire a memory that has no replacement, leave out the successor. Preview
first, and undo with `restore`:

```bash
"$PALINODE_BIN/palinode" archive decisions/harbor-notes-timestamps.md \
  --reason "Example only: no longer applies." --dry-run
"$PALINODE_BIN/palinode" restore decisions/harbor-notes-storage.md --dry-run
```

Both commands above are previews and write nothing; drop `--dry-run` to apply.

**Archive is not erasure.** An archived memory leaves default recall and search,
but the Markdown file, its `-history.md` audit sibling and its Git history all
remain, and `restore` brings it back. Removing data permanently, Git history
included, is a separate, destructive, manual procedure with no single command:
see [the erasure procedure](DATA-LIFECYCLE.md#the-erasure-procedure).

## 7. A conflict stays unresolved without evidence

Palinode records disagreement; it does not choose a winner for you. This
optional demonstration creates two disputed region choices and asks for their
current state:

```bash
"$PALINODE_BIN/palinode" save --type Decision --project harbor-notes \
  --slug harbor-notes-region-north --contradicts decisions/harbor-notes-region-south \
  "Deploy the shared service in the north region."
"$PALINODE_BIN/palinode" save --type Decision --project harbor-notes \
  --slug harbor-notes-region-south --contradicts decisions/harbor-notes-region-north \
  "Deploy the shared service in the south region."
"$PALINODE_BIN/palinode" resolve "harbor-notes deployment region"
```

The result lists both contested sides and says there is no winner. Do not use a
later save to silently resolve a conflict; add explicit evidence or a replacement
record when a decision is actually made.

## Next steps

- [Homebrew](HOMEBREW.md) for the current released installation and upgrades.
- [Operations](OPERATIONS.md) for service management, recovery, and model-mode behavior.
- [MCP install recipes](MCP-INSTALL-RECIPES.md) for client-specific configuration.
- [Privacy policy](PRIVACY.md) for local storage, optional remote services, and Git remotes.
- `palinode init --obsidian --dir /path/to/vault` to scaffold an Obsidian vault;
  it is optional and operates on the chosen vault, not on the code checkout.
