# Using Palinode from any harness

Palinode is one memory backend with several ways in. Your memories live in one
place — markdown files, git-versioned, indexed by one API server — and every
agent harness you use connects to that same store. Switch editors, run three at
once, move between machines: the memory follows you, not the tool.

This page is the map: what each harness gets, how deep the integration goes,
and where to start.

## Three integration tiers

Every integration is a thin client of the same REST API — no harness gets a
private fork of the capability set. What differs is how much of the memory
loop runs automatically:

| Tier | What runs without you asking | Where |
|------|------------------------------|-------|
| **Native hooks** | Session starts primed with core memories, relevant memory recalled before each prompt, and an eligible session-end floor can be submitted | Claude Code |
| **Native plugin** | The same full loop, wired into the harness's own extension API | Pi, Cline, OpenClaw |
| **MCP** | Explicit tools — the agent searches and saves when it decides to | Everything that speaks MCP |

The tiers stack. Claude Code users typically run hooks **and** MCP: hooks make
memory ambient, MCP tools let the agent dig deeper on demand.

## What each harness gets

| Harness | Tier | Setup |
|---------|------|-------|
| **Claude Code (CLI)** | Hooks + MCP | `palinode init` in your project — scaffolds everything below |
| **Claude Desktop** | MCP | `palinode mcp-config --stdio` → paste into its config |
| **Pi** | Native extension | [plugins/pi/README.md](../plugins/pi/README.md) — per-turn recall, priming, capture |
| **Cline (CLI / SDK)** | Native plugin | [plugins/cline/README.md](../plugins/cline/README.md) — per-turn recall, priming, capture; `cline plugin install` |
| **OpenClaw** | Native plugin + MCP | see the plugin's install guide ([plugin/INSTALL.md](../plugin/INSTALL.md)) |
| **Cursor** | MCP + rules file | `palinode mcp-config --stdio`; `palinode init` also writes `.cursor/rules/palinode.md` |
| **Windsurf** | MCP | `palinode mcp-config --stdio` (or `--http` for a remote server) |
| **Zed** | MCP | `palinode mcp-config --http` |
| **VS Code (Cline / Continue)** | MCP | [MCP-INSTALL-RECIPES.md](MCP-INSTALL-RECIPES.md) — the Cline VS Code extension does not load `AgentPlugin`s yet |
| **Codex CLI** | MCP + AGENTS.md | `~/.codex/config.toml`; `palinode init` appends a memory block to `AGENTS.md` |
| **Antigravity** | MCP + AGENTS.md | native MCP menu; same `AGENTS.md` block |
| **JetBrains (AI Assistant)** | MCP | [MCP-INSTALL-RECIPES.md](MCP-INSTALL-RECIPES.md) |

Per-client config file locations: [MCP-CONFIG-HOMES.md](MCP-CONFIG-HOMES.md).
If a setup misbehaves, `palinode mcp-config --diagnose` shows every config file
your clients actually read.

### Project scope when the server is on another machine

In the remote install, the agent's machine and the Palinode server are
different hosts. The server can't see the agent's working directory, and it
never treats its own directory as the agent's project. Each tier carries the
client's scope in its own way:

| Tier | How the client's project reaches the server |
|------|---------------------------------------------|
| **Hooks** (Claude Code) | The session-start prime and the per-turn resolve send the session's `cwd`; the server resolves the project from it the same way the controls check does. Set `PALINODE_PROJECT` in the hook environment to name the project explicitly instead. |
| **Native plugins** (Pi, Cline) | The per-turn resolve and the session-start prime send the workspace directory. |
| **MCP over stdio** | The MCP process runs on the client's machine, so its own directory (or `PALINODE_PROJECT`) is the client's. |
| **MCP over HTTP** | Send the `X-Palinode-Project` header: `palinode mcp-config --http --project <slug>` emits it. Without it, a call is unscoped, and the search output says `Scope: none (none)`. |

A request that carries no project is unscoped unless the operator set
`PALINODE_PROJECT` on the server, which applies to every client that sends
nothing.

**A project scope isolates as well as ranks.** Once a request's project is
resolved, whatever reaches the agent without being asked for (the per-turn
`POST /resolve`, the SessionStart prime) and default search (MCP
`palinode_search`, the CLI, REST, the plugin) leave out every record tagged to a
different project. A record is tagged to a different project when its
`entities` name at least one `project/*` and none of them is the request's
project. Records that name no project are global and are still delivered.
A record that names the request's project among several belongs to that project
too. An unscoped request is unchanged. Each payload counts what it left out:
`other_projects_withheld` on the resolve bundle and the prime digest, and in the
search receipt's `retrieval` block. That way an empty scoped result reads as
isolated, not as "nothing in memory". Reaching another project is an
explicit request, and the results come back labelled
`other project: project/<name>`: `palinode_search` / `palinode_resolve` with
`include_other_projects=true` (CLI `--include-other-projects`, REST
`{"include_other_projects": true}`). A record the caller names by `ref` is
always reported. The shipped hook and plugins never send the option.

Project names compare case-insensitively (`project/Orbit_App` is `orbit_app`'s). When
one project's records carry several spellings, group them in the store's curated
`entity-aliases.yaml` (the file entity lookup already reads): every member of a
`project/` group then counts as the group's canonical project, both on the
records and on the request. `palinode doctor` → `project_tags_unmapped` names
large tags that nothing covers, and `palinode aliases` edits the groups.
`context.project_map` keys still match a directory or repository name exactly.
[ENTITY-ALIASES.md](ENTITY-ALIASES.md) covers the file, which spellings to merge
and which to keep apart.

When isolation leaves a scoped result empty or down to one item, the text the
agent reads says so: `N records from other projects withheld (scope:
project/<p>). They are about other projects, not this one.` That includes search
output and the resolve bundle, and so the hook's injected context. The agent's
line deliberately names no way to see them: an agent told how fetches another
project's decision and answers with it. The CLI, read by a person, adds
`--include-other-projects to see them`. A full result carries no such line.

**Linked worktrees.** An agent working in a linked git worktree
(`.claude/worktrees/<task>`) has a directory named after the task. The shipped
hooks (prompt and session start) and the plugin core send the repository's main
worktree root as `cwd` instead: the parent of
`git rev-parse --git-common-dir` when that ends in `/.git`. A server on another
machine then resolves the repository's project rather than the task's. This is
best effort: with no `git`, or outside a work tree, the `cwd` goes as it is.

### When a project has both `.claude/CLAUDE.md` and `AGENTS.md`

`palinode init` writes the memory block to `.claude/CLAUDE.md`, and — when it
finds an `AGENTS.md` or a `.agent/` directory, or you pass `--agents` — also
writes a harness-neutral copy to `AGENTS.md`. Those are two files with two
audiences, not one file everything reads:

- **Claude Code** reads your `CLAUDE.md` files. Its documented default is to
  read them *instead of* `AGENTS.md` whenever a `CLAUDE.md`,
  `.claude/CLAUDE.md`, or `CLAUDE.local.md` exists in the working directory or
  above it; reading `AGENTS.md` directly requires Claude Code v2.1.277 or later
  ([memory docs](https://code.claude.com/docs/en/memory#agents-md)). The
  behavior is configurable in that client, including an option to load both.
- **Codex and other `AGENTS.md`-aware harnesses** read `AGENTS.md`. They do not
  read `CLAUDE.md`.

So in a project with both files, each block is live for one side and inert for
the other. **Maintain both.** Editing one does not change the other, and
re-running `palinode init` does not reconcile drift between them: it skips any
instruction file that already carries a Palinode section, and `--force` appends
a fresh block rather than replacing an edited one. To keep a single source of
truth instead, keep the block in one file and have the other reference it —
Claude Code expands `@path` imports from a `CLAUDE.md`. `palinode init` prints
this same note (in `--dry-run` too) whenever a run leaves both files in place.

## Flagship first-session proof

The two supported first-use paths are deliberately different. They share one
memory store and one sample, but only the Claude Code path has lifecycle hooks.
Do not treat an installed MCP server as a hook.

| Path | Start | During a prompt | Exit | Required setup |
|------|-------|-----------------|------|----------------|
| **Claude Code — hooks + MCP** | `SessionStart` provides its bounded digest automatically. | `UserPromptSubmit` can inject relevant trigger/search context; use MCP tools for a focused lookup or save. | `SessionEnd` attempts a minimal snapshot automatically when its floor applies; an agent-authored `palinode_session_end` is the richer explicit path. | First run `palinode controls status`, then run `palinode init --hook` in the project and approve the generated `.mcp.json` and `.claude/settings.json` hooks. |
| **Codex CLI — MCP + project instructions** | MCP connects, but it does not recall a decision on its own. | The project `AGENTS.md` must tell the agent to call `palinode_session_init`, then `palinode_search` when it needs an answer. | The agent must explicitly call `palinode_session_end`. | Add the MCP server through Codex's normal configuration and run `palinode init --agents` in the project. |

Client-owned memory may remain enabled in either client. Palinode is a separate,
auditable markdown store; it neither reads nor disables the client's own memory.
The release trial records whether both were enabled and what was observed. A
transport check alone cannot establish model behavior or coexistence.

### Shared harmless sample

Use project **`harbor-notes`** in a disposable project and store. The sample
contains no real user memory.

1. In session A, save this decision with its rationale: **“Use SQLite for the
   local prototype because it runs as a single-user desktop app.”**
2. Start a genuinely fresh session. Do not paste the answer into the prompt.
   Ask which storage choice fits the local prototype; retrieve the decision and
   read its source.
3. Save the explicit correction: **“Use PostgreSQL for the shared hosted
   service because concurrent writers need transactional coordination.”** Link
   it to the initial record, then supersede the initial record.
4. Start another fresh session and ask about the shared hosted service. It must
   retrieve PostgreSQL and show the linked source rather than silently choosing
   between records.

Also save **“Store event timestamps in UTC.”** and confirm it remains current.
For the conflict demonstration, save both a north-region and a south-region
deployment decision as contradictory records. Leave the conflict unresolved:
the client must report the disagreement, not invent a winner.

For the smallest reliable path, configure the store for lexical retrieval and
ask the agent to use `palinode_search` with the distinctive wording from the
decision. Lexical mode is an explicit configuration, not an automatic fallback
when hybrid embedding is unavailable. Embedding, chat, and consolidation are
optional and are not prerequisites for this proof.

### Prompts for the two paths

Claude Code's hooks make the first digest and prompt-time recall automatic
after setup. Use a fresh-session prompt such as: “For Harbor Notes, what
storage choice applies to the shared hosted service? Search Palinode and cite
the record you use.” This asks for an auditable lookup without supplying the
answer.

Codex CLI needs the same instruction explicitly in `AGENTS.md`: “At session
start call `palinode_session_init`; before answering a Harbor Notes storage
question call `palinode_search`; read the selected record for its source; at
the end call `palinode_session_end`.” The equivalent fresh-session prompt is:
“For Harbor Notes, determine the storage choice for the shared hosted service
from Palinode. Use the evidence you retrieve.”

The proof must be run once from the primary checkout and once from a linked
worktree after restarting the Palinode service. A linked worktree should resolve
to the same project; if it does not, stop and diagnose scope rather than adding
an arbitrary project override. Observe the `SessionEnd` floor separately: it
uses the basename of its `cwd`, so it can name the linked-worktree directory
rather than the primary checkout's project. Restarting a service is an
operational action, not a claim made by this guide.

### What a failure means

| Observation | Check first | Next safe action |
|-------------|-------------|------------------|
| Save fails | tool response and path validation | Correct the request; do not claim a file was saved. |
| Save succeeds but search misses it | `indexed`, `indexed_vec`, and `indexed_fts` in the save receipt | Wait for/re-run normal indexing; use the supported lexical query only after FTS is healthy. |
| Tools are absent or disconnected | the client's normal MCP listing/health view | Repair that client's configuration; do not edit another client's global config. |
| Fresh session has no project scope | session-init/prime output | Check the checkout or linked-worktree resolver before asking broader recall. |
| Search says no relevant memory | query the exact sample wording | Record the abstention; do not paste an answer or call it a recall success. |
| Embeddings are unavailable | search mode/receipt | Record the hybrid-mode failure. Only describe a lexical result when lexical mode was configured explicitly; it is separate from a semantic or model-session result. |

## Capture controls and what they cover

Before enabling automatic capture, run `palinode controls status`. It asks the
server for the effective policy and makes a content-free capture preflight; its
JSON form is suitable for a setup checklist. It reports observed generated hook
files/configuration separately from running-client state, which remains unknown
unless the client itself reports it. It also distinguishes Palinode-managed API
traffic from model/provider traffic managed by the client.

`palinode controls pause` pauses both future capture and recall requests routed
through the Palinode API. Use `pause --no-recall` for capture only, or
`pause --no-capture` for recall only; `resume` accepts the same selectors. Pauses do not stop background indexing,
enrichment, or consolidation; direct-file legacy clients; in-flight requests;
context already delivered to a client; or client-native memory and model
traffic.

`palinode controls exclude-project PROJECT` and `exclude-path PATH` exclude one
bounded project/path from automatic capture and recall; use `--remove` to undo
either. Exclusions intentionally do not refuse explicit saves or recalls, and
their tested scope does not constitute a general secret detector. A
failed/old/malformed policy preflight should make an automatic hook or client
decline capture or recall rather than fall through to a legacy path.

For the Claude Code fixture, transcript-derived floor capture is opt-in through
the selected hook setup, requires an eligible SessionEnd transcript, and stores
only a message count plus a truncated first-prompt topic hint—not a full
transcript import. Codex CLI MCP and `AGENTS.md` instructions do not turn on
transcript capture. Native plugins have their own lifecycle APIs, so the CLI can
only report their configuration if exposed; it cannot certify they are running.

### Mining transcripts for corrections

A separate, **opt-in** capture source reads harness session transcripts looking
for the one signal Palinode otherwise loses: the moment you told an agent it was
wrong.

**There are three separate opt-ins, and the third is the one that transmits
anything.** Turning the source on does not name a directory, and naming a
directory does not permit a model call:

1. `enabled` — turn the source on. Off by default.
2. `harness_paths` — name the directories it may read. Empty by default, so an
   enabled source with no paths still reads nothing.
3. `classify` — allow it to ask a model about what it found. **Off by default.**
   Detection is local arithmetic over files; classification is the only step
   that sends anything anywhere.

```yaml
capture:
  transcripts:
    enabled: true
    harness_paths:
      claude-code:
        - ~/.claude/projects
    classify: false      # detection only; nothing is sent to a model
    lookback_days: 7
    max_candidates: 50
```

**With `classify: false` (the default), a scan makes no request to any model
endpoint at all.** It runs the pattern stage locally and queues every candidate
as `needs_review` with a provenance that says the classifier was not run — which
is a different state from "a model was asked and could not be reached" and from
"a model answered and the answer was unusable". All three appear in the listing,
so a reviewer can tell which action each queue needs: turn classification on,
fix the endpoint, or re-run.

**What is read.** Only the paths you list, only `*.jsonl` files under them, and
only the turns *you* typed. Tool output replayed on a user line, compact
summaries, harness-injected reminders, subagent threads and the assistant's own
words are all excluded — as is text you quoted or pasted into your own turn, so
a correction inside a pasted document is not treated as your decision. Those
exclusions cover both halves of the pipeline: excluded text is neither made into
a candidate nor shown to the model (see "What is sent" below). Files are opened
read-only; nothing is ever written back to a harness directory.

**What is stored.** For each candidate: one bounded quoted span (at most a few
hundred characters of your own words), the session id, turn index and timestamp
it came from, the project scope, the classification, and — only when your words
actually said so — the reason you gave and the replacement you named. Candidates
live in `.palinode/correction-candidates.jsonl` in your store, which is
operational state: it is not indexed, not searchable, and not committed as
memory.

**What is never stored.** Whole turns, whole transcripts, file contents, tool
output, the surrounding conversation, or the transcript's path. The classifier's
window is discarded after the answer. The model chooses a label from a fixed set
and cannot write into the queue: every piece of text in a candidate is cut from
your own turn by the deterministic pass, so a model that replies with a paragraph
contributes none of it.

**What is sent, and where — only when `classify: true`.** With classification
off, this whole paragraph is inapplicable: nothing is transmitted, and the
disclosure says so. With it on, classifying a candidate **sends conversation
text to the model endpoint configured for consolidation**
(`consolidation.llm_url`).
**If that endpoint is not on this machine, that text leaves the machine.**
Specifically, per candidate: up to **5 turns** (the matched turn, two before and
two after), each truncated to **400 characters**. Of those turns, only **your own
turns and ordinary assistant replies** carry text, and they are stripped of
quoted lines, fenced/pasted blocks and harness-injected regions first — so a
document you pasted does not travel even though the turn around it does. Every
other kind of turn (tool output, subagent turns, summaries, harness metadata) is
replaced by a placeholder naming only its kind, e.g. `[tool output omitted]`.

The deterministic detection pass **sends nothing**; the classifier is the only
step that transmits anything, and when `classify: true` it runs for every
candidate a scan finds.

Whether it runs is a **configuration** decision, not a per-request one. No CLI
flag, request body or tool parameter can switch classification on for a single
call — a caller able to do that could send out text your config said stays
local. Change `capture.transcripts.classify` and restart, or leave it off.

To see where that is: `palinode controls status` reports the destination under
"Correction mining sends", and `GET /status` carries it as
`transcript_correction_capture.sends_to` beside the other configured endpoints.

**Nothing is applied.** Every candidate is a proposal. There is no path in this
release by which a mined correction changes, retires or writes a memory; the
review step that would do so does not exist yet. `palinode corrections` lists the
queue, `palinode corrections --scan` runs a detection pass, and the report says
so on every surface.

**Turning it off.** Set `enabled: false` (or remove the paths) and nothing is
read and nothing is sent again; delete `.palinode/correction-candidates.jsonl`
and nothing remains.
A scan also honours the controls above exactly as every other automatic source
does: paused capture stops it before the first read, and an excluded project or
path skips those transcripts. The lookback window and the candidate cap both
report what they skipped — a bounded scan says how much it left behind.

Use the harmless sequence `controls pause` → inspect the local
[UI](UI.md)/`history` → attempt a new capture or recall and observe the policy
denial → `controls resume`. The fictional Harbor Notes walkthrough above remains
the recall proof; it is not a human-trial claim about pause or exclusion success.

## The full loop, on Claude Code

Claude Code is the deepest integration today because its hook system covers
the whole session lifecycle. After `palinode init`:

1. **Session start** — the `SessionStart` hook injects a bounded digest of
   your `core: true` memories, so the session begins already knowing your
   standing context.
2. **Every prompt** — the `UserPromptSubmit` hook checks your prospective
   triggers (`palinode_trigger`) and runs a strict-threshold search over the
   prompt, injecting compact snippets *before the model answers*. Nothing
   relevant → it says nothing.
3. **On demand** — the MCP tools (`palinode_search`, `palinode_read`,
   `palinode_save`, …) are there when the agent wants more than the ambient
   layer surfaced.
4. **Session end** — the `SessionEnd` hook can submit a minimal floor snapshot
   on its configured end reasons. It requires a transcript, at least three user
   messages, and no existing `palinode_session_end` call; it uses the checkout
   basename as the project and only the first prompt's topic/count. It is not a
   guarantee that every session is captured, and the explicit tool call remains
   the richer record.

Tuning knobs for all three hooks: [examples/hooks/](../examples/hooks/).

### Why injected memory lands in the conversation, not the system prompt

Model providers cache your prompt as a strict prefix: tools, then system
prompt, then messages. Change one byte early in that prefix and everything
after it is re-processed — and re-billed — from scratch. Per-turn memory
injected into the *system prompt* would do exactly that, every turn.

Palinode's hooks inject into the **conversation** instead, after the cached
prefix. The recall arrives fresh each turn; the expensive stable prefix stays
cached. Every native plugin follows the same rule — the Pi and Cline plugins
share one core (`plugins/core`) whose only injection output is a message
body, and each plugin's test suite pins that it never lands in the system
prompt.

## Coexisting with a client's own memory

Some clients keep a memory of their own: Claude Code's auto-memory (a
`MEMORY.md` per project under `~/.claude/projects/`), and Codex CLI's memories
when you turn them on (they are off by default). Palinode and a client's native
memory can both be enabled at once.

- **Palinode never writes a client's native memory.** Its only write path is
  its own store. It reads client files only when you ask it to:
  `palinode migrate` imports an OpenClaw `MEMORY.md`, and opt-in transcript
  capture ([above](#mining-transcripts-for-corrections)) reads Claude Code
  session transcripts. Measured on Claude Code 2.1.119 and Codex CLI 0.156.1
  in a live-agent evaluation: no native memory file changed because of
  Palinode in any run, checked by content hash before and after. The native
  file did change when the client itself chose to rewrite it, which is the
  client's own behaviour.
- **The two are not reconciled.** Nothing merges them, and when they disagree,
  agents in the same evaluation did not reliably say so. They picked one
  source without mentioning the other. If a fact matters, keep it in one
  place. A standing rule you want an agent to follow unconditionally belongs
  in the client's instruction file (`CLAUDE.md` / `AGENTS.md`). Palinode
  memory is delivered as reference data, and a careful model weighs it
  against what it can verify. See
  [SECURITY.md#memory-poisoning-and-trust-limitations](../SECURITY.md#memory-poisoning-and-trust-limitations)
  for what that framing does and does not protect against, measured.
- **Turning one off doesn't affect the other.** Disabling Claude Code's
  auto-memory, or pausing Palinode's capture or recall (`palinode controls`),
  changes only that system.

## Same contract everywhere

Whatever the tier, the memory contract is identical, because everything calls
the same API:

- **Save with rationale** — decisions carry their why (`palinode_save`).
- **Recall is search, not scrollback** — hybrid keyword + semantic search
  over everything you've ever saved (`palinode_search`).
- **Retired memories stay out of automatic delivery** — what a harness is
  handed without asking (the per-turn hook's or plugin's `POST /resolve`, the
  SessionStart prime) leaves archived, superseded, retracted and expired
  records out, as default search does, so every integration agrees. A current
  record that replaced one is still delivered, with `replaces: 1 earlier
  record (retired; withheld)` rather than the old value. History is an
  explicit request and comes back labelled: `palinode_resolve` with
  `include_retired=true` (CLI `--include-retired`, REST
  `{"include_retired": true}`), `palinode history`, `palinode trace`. See
  [DATA-LIFECYCLE.md](DATA-LIFECYCLE.md#what-stops-being-offered-means-on-each-path).
- **Sessions end captured** — `palinode_session_end` (or the Claude Code
  floor hook) writes the session's outcomes where the next session will find
  them.
- **Everything is auditable** — files + git means `diff`, `blame`, and
  `rollback` work on your agent's memory like on your code.

A memory saved from Cursor is findable from Claude Code, from a cron job via
the CLI, from OpenClaw — the harness is a doorway, not a silo.

## More native integrations

The plugin architecture is deliberately thin — a native integration is an
adapter over the REST API, not a reimplementation — so support for more
harnesses with lifecycle hooks is planned. If your harness of choice exposes a
pre-prompt hook and you want Palinode wired into it, open an issue.
