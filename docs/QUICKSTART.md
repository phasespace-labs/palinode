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

Exclusions do not block an explicit save, explicit recall, or other explicit
user/API input, and they are not a secret-scanner guarantee. Do not submit
secrets as explicit content. Transcript capture remains an opt-in harness
feature; MCP installation alone does not enable it, and the command cannot
certify a client process is running. See [HARNESSES.md](HARNESSES.md) for source/range and
automatic-versus-explicit behavior, and [PRIVACY.md](PRIVACY.md) for visibility
and access limits.

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

The local inspector is read-only and loopback-only. It lets you browse the
saved decision and its history; it is not an authorization boundary. Visit
<http://127.0.0.1:6340/ui/> in a browser (`open` on macOS, `xdg-open` on many
Linux desktops, or paste the URL on Windows). See [UI.md](UI.md) for its
visibility and deployment limits.

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
