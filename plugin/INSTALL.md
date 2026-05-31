# openclaw-palinode — Installation & Host-Side Opt-in Flags

## Prerequisites

- **Palinode API server** running and reachable (default: `http://localhost:6340`). See the root-level `README.md` for server setup.
- **OpenClaw 2026.5.x** or later.

## Installing the plugin

For bundled releases, the plugin is included automatically. For non-bundled installs, copy or symlink the `plugin/` directory into your OpenClaw plugins folder and reference it from your host config.

## Host-side opt-in flags (required)

Non-bundled plugins on OpenClaw 2026.5.x load successfully — showing `Status: loaded` — but run with **tools and hooks silently disabled** unless you explicitly grant them in the host config. There is no error message. The plugin will appear to work, but `autoRecall` will never fire and none of the `palinode_*` tools will be reachable from the agent.

Add the following block to your OpenClaw host config under `plugins.entries.openclaw-palinode`:

```json
{
  "plugins": {
    "entries": {
      "openclaw-palinode": {
        "hooks": {
          "allowPromptInjection": true,
          "allowConversationAccess": true
        }
      }
    }
  }
}
```

### Why each flag is required

| Flag | Required for | Effect without it |
|------|-------------|-------------------|
| `allowPromptInjection` | `autoRecall` — the `before_prompt_build` hook that prepends Palinode memory context to every agent turn | Hook is registered but its `prependContext` return value is silently dropped; the agent receives no injected memory |
| `allowConversationAccess` | Reading `event.prompt` inside the `before_prompt_build` hook to build the semantic recall query | Hook fires but `event.prompt` is `undefined`; semantic/associative/trigger recall is skipped, only core file injection can run |

Both flags are required for full functionality. `allowConversationAccess` alone gives you the recall query; `allowPromptInjection` alone lets you inject — you need both for autoRecall to work end-to-end.

> **Note:** These flags cover the `before_prompt_build`, `after_compaction`, `agent_end`, and `before_reset` hooks used by this plugin. The `palinode_*` tools (`palinode_search`, `palinode_save`, `palinode_ingest`, `palinode_status`, `palinode_diff`, `palinode_blame`, `palinode_depends`) are registered separately via `api.registerTool` and do not require hook flags — they are available to the agent as tools as long as the plugin is loaded.

## Verifying the install

### 1. Check the gateway log at startup

When the plugin initializes successfully with hooks enabled, you should see a line like:

```
openclaw-palinode: registered (api: http://localhost:6340, dir: ~/palinode, autoRecall: true, autoCapture: true)
```

If that line appears but you see no subsequent `openclaw-palinode: turn 1 — profile=...` line on the first agent turn, the `before_prompt_build` hook is not firing — check that `allowPromptInjection` is set to `true` in the host config.

### 2. Confirm the agent quotes real injected memory

Send the agent a message that should match something in your Palinode memory store. If autoRecall is working, the agent's reply will reference specific facts from your memory files. If the agent's answer looks generic or hallucinates details that contradict your actual memory files, the context injection is not reaching it.

You can also ask the agent directly:

> "What context did you receive from Palinode at the start of this session?"

A working install will quote the `<palinode-memory profile="...">` block contents. A misconfigured one will say it received nothing.

### 3. Verify tool availability

Ask the agent to call `palinode_status`. It should return file counts and index health from your Palinode server. A `tool not found` error means the plugin did not register, not a hook issue — check that the plugin loaded at all (look for the `registered` log line).

## Plugin config fields

These live under `plugins.entries.openclaw-palinode` alongside the `hooks` block:

| Field | Default | Description |
|-------|---------|-------------|
| `palinodeApiUrl` | `http://localhost:6340` | Palinode API server URL |
| `palinodeDir` | `~/palinode` | Path to your Palinode memory directory |
| `autoRecall` | `true` | Inject core memory + semantic recall before each agent turn |
| `autoCapture` | `true` | Append session summaries to `daily/` at agent end |
| `recallProfile` | `coding` | Named recall preset: `coding`, `monitoring`, `investigation`, `writing`, `conversation`, `minimal`, `off` |
| `recallProfileConfig` | — | Per-field overrides on top of the named preset |
| `promptsDir` | `specs/prompts` | Path to extraction prompts, relative to `palinodeDir` |
