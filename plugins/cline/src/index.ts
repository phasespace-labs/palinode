/**
 * Palinode plugin for Cline (`AgentPlugin`) — the thin binding.
 *
 * Per ADR-019, a Palinode plugin is a delivery adapter over the REST API:
 * this file wires two Cline runtime hooks to the pure client logic in the
 * shared core (`plugins/core`, compiled into this package's dist) and
 * contains no capability of its own.
 *
 *   beforeModel → session priming (first model call of the session) +
 *                 per-turn recall for the newest user prompt, injected as a
 *                 `role: "user"` MESSAGE right after that prompt
 *   afterRun    → capture floor via /session-end
 *
 * Built against the Cline SDK's runtime-native plugin hooks (`@cline/sdk`
 * 0.0.75; `sdk/packages/shared/src/agent.ts` → `AgentRuntimeHooks`, and
 * `sdk/packages/agents/src/agent-runtime.ts` for the call sites). The
 * docs page (docs.cline.bot/sdk/plugins) names the same seven hooks —
 * beforeRun/afterRun/beforeModel/afterModel/beforeTool/afterTool/onEvent;
 * its "Hook Stages" (`before_agent_start`, `run_end`) and "Hook Policies"
 * (`fail_closed`, retries) tables describe nothing that exists on
 * `AgentPlugin` in that SDK, so this plugin does not rely on them: the
 * `before_agent_start` role is played by `beforeModel`, `run_end` by
 * `afterRun`, and fail-open is enforced HERE, not by a host policy.
 *
 * Facts this binding leans on (all verified in the SDK source):
 *   - `beforeModel({ snapshot, request })` may return `{ messages }`; the
 *     runtime replaces `request.messages` with it and reads nothing else that
 *     could reach the system prompt (`AgentBeforeModelResult` has no
 *     `systemPrompt`). The message array is the ONLY thing this hook can
 *     inject into — exactly the ADR-019 §4 shape. `test/binding.test.ts`
 *     pins that the returned object never carries `systemPrompt` and that
 *     `request.systemPrompt` is untouched.
 *   - `beforeModel` fires on EVERY model call — every tool-loop iteration,
 *     not once per user turn — and its result is applied to the request only,
 *     never persisted into the runtime's message state. So the recall for a
 *     prompt is computed once and re-inserted, byte-identical and at the same
 *     position, on every later request of the session. Anything else would
 *     shift the cached prefix on the next call and cost more than it saves.
 *   - A hook that throws fails the run (`status: "failed"`), and a sandboxed
 *     plugin (anything installed with `cline plugin install`) has a default
 *     3000 ms hook timeout after which the sandbox process is torn down. So
 *     every hook here is wrapped: any error → undefined, and the whole
 *     recall races a deadline comfortably inside that budget.
 *   - `AgentMessageRole` is "user" | "assistant" | "tool" — there is no
 *     system role in Cline's message array, so injected memory is a `user`
 *     message. Cline itself inserts synthetic consecutive user messages
 *     (`split-tool-images.ts`), so the shape is a known-good one.
 */

import { basename } from "node:path";
import {
  automaticAllowed,
  buildCoreDigest,
  buildRecallContext,
  buildSessionCapture,
  configFromEnv,
  postSessionCapture,
  userEntries,
  type PalinodeConfig,
} from "../../core/src/index.js";

/** Whole-hook deadline. Cline's plugin sandbox tears the plugin process down
 *  at 3000 ms (`plugin-sandbox.ts`, `hookTimeoutMs` default) — the recall
 *  must resolve, or give up, well inside that. Fail-open, not fail-slow. */
export const DEFAULT_HOOK_DEADLINE_MS = 2000;

/** Structural types for the slice of Cline's plugin API this plugin touches
 *  (`@cline/shared` agent.ts / contribution-registry.ts). Structural on
 *  purpose: no dependency on `@cline/sdk`, so the plugin builds and tests
 *  standalone and stays installable as a plain package. */
export interface ClineTextPart {
  type: "text";
  text: string;
}
/** The slice of Cline's `AgentMessage` this plugin reads. The hooks are
 *  generic over the host's concrete message type (`M extends MessageLike`)
 *  so the plugin type-checks against the real `AgentPlugin` without naming it. */
export interface MessageLike {
  id: string;
  role: string;
  content: ReadonlyArray<{ type: string }>;
  createdAt: number;
  metadata?: Record<string, unknown>;
}
/** Concrete message shape used by the tests (a valid `MessageLike`). */
export interface ClineMessage extends MessageLike {
  role: "user" | "assistant" | "tool";
  content: Array<ClineTextPart | { type: string }>;
}
export interface ClineSnapshot {
  agentId?: string;
  parentAgentId?: string | null;
  runId?: string;
  iteration?: number;
  messages?: readonly MessageLike[];
}
export interface BeforeModelContext<M extends MessageLike> {
  snapshot: ClineSnapshot;
  request: { systemPrompt?: string; messages: readonly M[] };
}
export interface BeforeModelResult<M extends MessageLike> {
  messages: M[];
}
export interface AfterRunContext {
  snapshot: ClineSnapshot;
  result: { status?: string; messages?: readonly MessageLike[] };
}
export interface PluginSetupContext {
  session?: { sessionId?: string };
  workspaceInfo?: { rootPath?: string };
  /** Cline's `BasicLogger`: `log` for operational messages, `debug` for verbose. */
  logger?: { log: (message: string) => void; debug: (message: string) => void };
}
export interface ClinePlugin {
  name: string;
  manifest: { capabilities: Array<"hooks"> };
  setup?: (api: unknown, ctx: PluginSetupContext) => void;
  hooks: {
    beforeModel: <M extends MessageLike>(
      ctx: BeforeModelContext<M>,
    ) => Promise<BeforeModelResult<M> | undefined>;
    afterRun: (ctx: AfterRunContext) => Promise<void>;
  };
}

export interface PalinodePluginOptions extends Partial<PalinodeConfig> {
  /** Override the whole-hook deadline (ms). Env: PALINODE_CLINE_HOOK_DEADLINE_MS. */
  hookDeadlineMs?: number;
  /** Test seam: env to read knobs from (defaults to process.env). */
  env?: Record<string, string | undefined>;
  /** Test seam: fetch implementation (defaults to global fetch). */
  fetchFn?: typeof fetch;
}

/** Marker every injected message carries; also what keeps them out of the
 *  capture floor's user-message count should a host ever persist them. */
export const INJECTED_KIND = "palinode-recall";

function isTextPart(p: { type: string }): p is ClineTextPart {
  return p.type === "text" && typeof (p as ClineTextPart).text === "string";
}

function promptText(m: MessageLike): string {
  return m.content.filter(isTextPart).map((p) => p.text).join("\n");
}

function lastUserPrompt<M extends MessageLike>(messages: readonly M[]): M | undefined {
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    if (m.role !== "user") continue;
    if (m.metadata?.kind === INJECTED_KIND) continue;
    if (promptText(m).trim() !== "") return m;
  }
  return undefined;
}

async function withDeadline<T>(work: Promise<T>, ms: number): Promise<T | null> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const deadline = new Promise<null>((resolve) => {
    timer = setTimeout(() => resolve(null), ms);
  });
  try {
    return await Promise.race([work, deadline]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

/**
 * Build the plugin. `createPalinodePlugin()` with no arguments reads the
 * shared env knobs; SDK users can pass overrides (`apiUrl`, `token`,
 * `threshold`, `recallProfile`, …) directly.
 */
export function createPalinodePlugin(options: PalinodePluginOptions = {}): ClinePlugin {
  const { hookDeadlineMs, env = process.env, fetchFn = fetch, ...overrides } = options;
  const cfg = configFromEnv(env, overrides);
  const deadlineMs =
    hookDeadlineMs ??
    (Number(env.PALINODE_CLINE_HOOK_DEADLINE_MS) > 0
      ? Number(env.PALINODE_CLINE_HOOK_DEADLINE_MS)
      : DEFAULT_HOOK_DEADLINE_MS);

  let sessionId = "";
  let cwd = process.cwd();
  let project = basename(cwd);
  let primed = false;
  /** Prompt-message id → the injected message that follows it (null = nothing
   *  to say, remembered so a silent prompt is never re-queried). Replayed
   *  into every request so the cached prefix stays byte-stable. */
  const ledger = new Map<string, ClineMessage | null>();
  /** Root-agent user-message count at the last capture; 0 = never. */
  let capturedAt = 0;

  async function contextFor(prompt: MessageLike): Promise<string | null> {
    const digest = primed ? Promise.resolve(null) : buildCoreDigest(cfg, fetchFn, cwd, sessionId);
    primed = true;
    const parts = await Promise.all([digest, buildRecallContext(promptText(prompt), cfg, fetchFn, cwd)]);
    const text = parts.filter((p): p is string => Boolean(p)).join("\n\n");
    return text || null;
  }

  return {
    name: "palinode",
    manifest: { capabilities: ["hooks"] },

    setup(_api, ctx) {
      sessionId = ctx.session?.sessionId ?? "";
      if (ctx.workspaceInfo?.rootPath) {
        cwd = ctx.workspaceInfo.rootPath;
        project = basename(cwd);
      }
      ctx.logger?.log(
        `palinode: registered (api: ${cfg.apiUrl}, profile: ${cfg.recallProfile}, threshold: ${cfg.threshold})`,
      );
    },

    hooks: {
      async beforeModel<M extends MessageLike>({ snapshot, request }: BeforeModelContext<M>) {
        try {
          // Root agent only: subagents (agents-squad) share the plugin, and
          // recall injected into every subagent's every request is a cost
          // multiplier with no memory upside.
          if (snapshot.parentAgentId) return undefined;
          if (!await withDeadline(automaticAllowed(cfg, "recall", cwd, fetchFn), deadlineMs)) {
            ledger.clear();
            primed = false;
            return undefined;
          }

          const prompt = lastUserPrompt(request.messages);
          if (prompt && !ledger.has(prompt.id)) {
            // Reserve the slot first so a slow API is not re-queried on the
            // next iteration of the same run.
            ledger.set(prompt.id, null);
            const text = await withDeadline(contextFor(prompt), deadlineMs);
            if (text) {
              ledger.set(prompt.id, {
                id: `${INJECTED_KIND}-${prompt.id}`,
                role: "user",
                createdAt: prompt.createdAt,
                metadata: { kind: INJECTED_KIND },
                content: [{ type: "text", text }],
              });
            }
          }

          // Replay: every prompt that has recall gets it re-attached, in the
          // same place, on every request. Nothing recalled anywhere → return
          // undefined so the request is untouched (silence is free).
          let changed = false;
          const messages: M[] = [];
          for (const m of request.messages) {
            messages.push(m);
            const injected = ledger.get(m.id);
            if (injected) {
              // Structurally a host message (id/role/createdAt/content
              // parts); the host type is only known generically here.
              messages.push(injected as unknown as M);
              changed = true;
            }
          }
          if (changed && !await withDeadline(automaticAllowed(cfg, "recall", cwd, fetchFn), deadlineMs)) {
            ledger.clear();
            primed = false;
            return undefined;
          }
          return changed ? { messages } : undefined;
        } catch {
          return undefined; // fail-open: Palinode trouble never blocks a turn
        }
      },

      async afterRun({ snapshot, result }) {
        try {
          if (snapshot.parentAgentId || !cfg.captureOn ||
            !await withDeadline(automaticAllowed(cfg, "capture", cwd, fetchFn), deadlineMs)) return;
          const entries = [...(result.messages ?? snapshot.messages ?? [])].filter(
            (m) => m.metadata?.kind !== INJECTED_KIND,
          );
          // Cline has no session-shutdown hook for plugins, so the floor is
          // progressive: first when the session crosses the message floor,
          // then again each time the count doubles — bounded, and the latest
          // capture always covers at least half the session.
          const count = userEntries(entries).length;
          if (count < cfg.minMessages || count < capturedAt * 2) return;
          const payload = buildSessionCapture(entries, cfg, {
            project,
            source: "cline-plugin",
            harness: "cline",
            trigger: "run_end",
            sessionId: sessionId || undefined,
            cwd,
          });
          if (!payload) return;
          if (await withDeadline(postSessionCapture(payload, cfg, fetchFn), deadlineMs)) capturedAt = count;
        } catch {
          // fail-open
        }
      },
    },
  };
}

/** Default export: the plugin with env-driven config, ready for
 *  `cline plugin install` / `pluginPaths` / `extensions: [plugin]`. */
const plugin = createPalinodePlugin();
export default plugin;
