# palinode-plugin-core

The shared TypeScript core for Palinode's harness plugins (`plugins/pi`,
`plugins/cline`). Extracted on the third plugin, per the rule recorded in
ADR-019 §4: two implementations do not reveal which parts are common and
which are harness-shaped; three do.

It contains exactly what the plugins duplicated before extraction, and
nothing hook-shaped:

| Export | What it is |
|--------|------------|
| `configFromEnv(env, overrides)` | The shared knobs (`PALINODE_API_URL`, `PALINODE_API_TOKEN`, `PALINODE_HOOK_RECALL_*`, `PALINODE_HOOK_INJECT_*`, `PALINODE_HOOK_MIN_MESSAGES`) plus `PALINODE_HOOK_RECALL_PROFILE`; harness config overrides the env |
| `PROFILES` | Recall profiles (`coding`, `monitoring`, `investigation`, `writing`, `conversation`, `minimal`, `off`) — the OpenClaw plugin's vocabulary, expressed as which channels are on |
| `apiJson(cfg, fetch, path, init)` | The fail-open REST client: bearer, timeout, HTTP ≥ 400 → `null`, never throws |
| `buildRecallContext(prompt, cfg, fetch)` | Per-turn recall: fired triggers + strict-threshold search → one bounded text block, or `null` |
| `buildCoreDigest(cfg, fetch, cwd, sessionId)` | Session-start priming: warm `/context/prime`, digest of `core: true` memories, or `null` |
| `buildSessionCapture(entries, cfg, origin)` / `postSessionCapture` | The capture-floor payload for `/session-end`, over Pi- or Cline-shaped message entries |

## The one invariant this core owns

Everything this module produces for injection is a **message body**. There
is no function here that yields a system prompt, and no binding may route
these strings into one (ADR-019 §4). Model providers cache the prompt
as a strict prefix — tools, then system, then messages — so per-turn content
in the system prompt invalidates the whole cached prefix every turn and
costs more than the recall saves. Bindings append a message after the cached
prefix instead, and each binding's own test suite pins that.

## How it is consumed

Not published to npm. Each plugin compiles the core into its own `dist/`
(`tsconfig` `rootDir: ".."`, `include: ["src/**/*.ts", "../core/src/**/*.ts"]`),
so an installed plugin has no runtime dependency on this directory. Import
it by relative path: `import { ... } from "../../core/src/index.js"`.

```bash
npm install
npm test        # vitest — pure functions over an injected fetch
```
