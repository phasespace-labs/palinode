/**
 * Palinode extension for the Pi coding agent — the thin binding.
 *
 * Per ADR-019, a Palinode plugin is a delivery adapter over the REST API:
 * this file wires three lifecycle events to the pure client logic in the
 * shared core (`plugins/core`, compiled into this package's dist) and
 * contains no capability of its own.
 *
 *   session_start      → warm /context/prime + queue a core-memory digest
 *                        for the next prompt (deliverAs: "nextTurn" — never
 *                        interrupts, never triggers a turn)
 *   before_agent_start → per-turn recall (triggers + strict search),
 *                        injected as a MESSAGE
 *   session_shutdown   → capture floor via /session-end
 *
 * THE ONE RULE THAT MUST SURVIVE EVERY REFACTOR: recall is returned as
 * `{ message }` and NEVER as `{ systemPrompt }`. Pi's `before_agent_start`
 * happily accepts a system-prompt replacement, and it is the trap ADR-019
 * §4 names: the model provider's prompt cache is a strict prefix match, so
 * per-turn content in the system prompt invalidates the entire cached
 * prefix (tools + system + history) on every turn — costing more than the
 * recall saves. Messages land after the cached prefix; the cache survives.
 * `test/binding.test.ts` pins this.
 */

import { basename } from "node:path";
import {
  automaticAllowed,
  buildCoreDigest,
  buildRecallContext,
  buildSessionCapture,
  configFromEnv,
  postSessionCapture,
  type SessionEntryLike,
} from "../../core/src/index.js";

/** Structural types for the slice of Pi's ExtensionAPI this extension
 *  touches. Structural on purpose: no dependency on Pi's own package, so
 *  the extension builds and tests standalone. */
interface BeforeAgentStartEvent {
  prompt?: string;
}
interface PiContext {
  sessionManager?: {
    getEntries?: () => SessionEntryLike[];
    getSessionId?: () => string;
  };
}
type HandlerResult =
  | { message: { customType: string; content: string; display: boolean } }
  | undefined;
export interface PiLike {
  on(
    event: "session_start" | "session_shutdown",
    handler: (event: unknown, ctx: PiContext) => void | Promise<void>,
  ): void;
  on(
    event: "before_agent_start",
    handler: (
      event: BeforeAgentStartEvent,
      ctx: PiContext,
    ) => HandlerResult | Promise<HandlerResult>,
  ): void;
  sendMessage(
    message: { customType: string; content: string; display: boolean },
    options?: { deliverAs?: "steer" | "followUp" | "nextTurn"; triggerTurn?: boolean },
  ): void;
}

export default function palinode(pi: PiLike): void {
  const cfg = configFromEnv();

  pi.on("session_start", async (_event, ctx) => {
    if (!await automaticAllowed(cfg, "recall", process.cwd())) return;
    const sessionId = ctx.sessionManager?.getSessionId?.() ?? "";
    const digest = await buildCoreDigest(cfg, fetch, process.cwd(), sessionId);
    if (digest && await automaticAllowed(cfg, "recall", process.cwd())) {
      // nextTurn: queued for the next user prompt — does not interrupt,
      // does not trigger a turn. Session-start priming, Pi-shaped.
      pi.sendMessage(
        { customType: "palinode-prime", content: digest, display: true },
        { deliverAs: "nextTurn" },
      );
    }
  });

  pi.on("before_agent_start", async (event, _ctx) => {
    if (!await automaticAllowed(cfg, "recall", process.cwd())) return;
    const context = await buildRecallContext(event.prompt ?? "", cfg, fetch, process.cwd());
    if (!context || !await automaticAllowed(cfg, "recall", process.cwd())) return undefined; // silence is the common case
    return {
      message: {
        customType: "palinode-recall",
        content: context,
        display: true,
      },
    };
  });

  pi.on("session_shutdown", async (_event, ctx) => {
    if (!cfg.captureOn || !await automaticAllowed(cfg, "capture", process.cwd())) return;
    const entries = ctx.sessionManager?.getEntries?.() ?? [];
    const payload = buildSessionCapture(entries, cfg, {
      project: basename(process.cwd()),
      source: "pi-extension",
      harness: "pi",
      trigger: "session_shutdown",
      sessionId: ctx.sessionManager?.getSessionId?.() || undefined,
      cwd: process.cwd(),
    });
    if (payload) await postSessionCapture(payload, cfg);
  });
}
