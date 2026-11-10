/** The Pi binding: lifecycle wiring, and the one invariant that must
 *  survive every refactor — recall is injected as a MESSAGE, never as a
 *  system prompt. */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import palinode, { type PiLike } from "../src/index.js";

type Handler = (event: unknown, ctx: unknown) => unknown;

function fakePi() {
  const handlers = new Map<string, Handler>();
  const sent: Array<{ message: unknown; options: unknown }> = [];
  const pi = {
    on: (event: string, handler: Handler) => {
      handlers.set(event, handler);
    },
    sendMessage: (message: unknown, options?: unknown) => {
      sent.push({ message, options });
    },
  } as unknown as PiLike;
  return { pi, handlers, sent };
}

const emptyCtx = { sessionManager: undefined };

beforeEach(() => {
  vi.stubEnv("PALINODE_CAPTURE_ENABLED", "1");
  // Default stub: every Palinode endpoint quietly empty.
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: unknown) => {
      const u = String(url);
      if (u.includes("/controls/check")) return Response.json({ allowed: true });
      if (u.includes("/search")) return new Response(JSON.stringify({ results: [] }));
      return new Response("[]");
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("registration", () => {
  it("binds exactly the three lifecycle events", () => {
    const { pi, handlers } = fakePi();
    palinode(pi);
    expect([...handlers.keys()].sort()).toEqual([
      "before_agent_start",
      "session_shutdown",
      "session_start",
    ]);
  });
});

describe("before_agent_start", () => {
  it("keys isolation-only delivery on the Pi session manager's id", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: unknown) => {
      if (String(url).includes("/controls/check")) return Response.json({ allowed: true });
      if (String(url).includes("/resolve")) return Response.json({
        selected: [], conflicts: [], replaced: [], insufficient: [], omitted_conflicts: 0,
        other_projects_withheld: 2, retrieval: { confidence: "confident" },
        text: "2 records from other projects withheld (scope: project/alpha).",
      });
      return Response.json([]);
    }));
    const { pi, handlers } = fakePi();
    palinode(pi);
    const turn = (sessionId: string) => handlers.get("before_agent_start")!(
      { prompt: "what did we decide about the rollback path?" },
      { sessionManager: { getSessionId: () => sessionId } },
    );
    expect(await turn("pi-isolation-a")).toHaveProperty("message");
    expect(await turn("pi-isolation-a")).toBeUndefined();
    expect(await turn("pi-isolation-b")).toHaveProperty("message");
    expect(await turn("pi-isolation-a")).toBeUndefined();
  });

  it("returns recall as a message — and NEVER a systemPrompt", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: unknown) => {
        const u = String(url);
      if (u.includes("/controls/check")) return Response.json({ allowed: true });
        if (u.includes("/search"))
          return new Response(
            JSON.stringify({
              results: [{ rel_path: "decisions/x.md", score: 0.9, snippet: "the decision" }],
              receipt: { retrieval: { confidence: "confident" } },
            }),
          );
        return new Response("[]");
      }),
    );
    const { pi, handlers } = fakePi();
    palinode(pi);
    const result = (await handlers.get("before_agent_start")!(
      { prompt: "what did we decide about the rollback path?" },
      emptyCtx,
    )) as Record<string, unknown>;

    expect(result.message).toMatchObject({ customType: "palinode-recall" });
    expect(String((result.message as { content: string }).content)).toContain("decisions/x.md");
    // The load-bearing assertion: per-turn system-prompt mutation would
    // invalidate the provider's cached prefix every turn. If this key ever
    // appears, the extension has become more expensive than no extension.
    expect(result).not.toHaveProperty("systemPrompt");
  });

  it("returns undefined when nothing is recalled", async () => {
    const { pi, handlers } = fakePi();
    palinode(pi);
    const result = await handlers.get("before_agent_start")!(
      { prompt: "what did we decide about the rollback path?" },
      emptyCtx,
    );
    expect(result).toBeUndefined();
  });

  it("returns undefined (not an error) when the API is down", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new Error("connection refused");
      }),
    );
    const { pi, handlers } = fakePi();
    palinode(pi);
    const result = await handlers.get("before_agent_start")!(
      { prompt: "what did we decide about the rollback path?" },
      emptyCtx,
    );
    expect(result).toBeUndefined();
  });
});

describe("session_start", () => {
  it("queues the core digest as a nextTurn message — never interrupts", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: unknown) => {
        const u = String(url);
      if (u.includes("/controls/check")) return Response.json({ allowed: true });
        if (u.includes("/context/prime"))
          return Response.json({ core_memories: [{ file: "projects/p.md", summary: "P — s" }] });
        return new Response("{}");
      }),
    );
    const { pi, handlers, sent } = fakePi();
    palinode(pi);
    await handlers.get("session_start")!({}, emptyCtx);

    expect(sent).toHaveLength(1);
    expect(sent[0].message).toMatchObject({ customType: "palinode-prime" });
    expect(sent[0].options).toMatchObject({ deliverAs: "nextTurn" });
  });

  it("stays silent with no core memories", async () => {
    const { pi, handlers, sent } = fakePi();
    palinode(pi);
    await handlers.get("session_start")!({}, emptyCtx);
    expect(sent).toHaveLength(0);
  });
});

describe("session_shutdown", () => {
  const entries = Array.from({ length: 3 }, (_, i) => ({
    type: "message",
    message: { role: "user", content: `prompt ${i}` },
  }));

  it("posts the capture floor from session entries", async () => {
    const posts: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: unknown, init?: { body?: unknown }) => {
        if (String(url).includes("/controls/check")) return Response.json({ allowed: true });
        posts.push(String(url) + " " + String(init?.body ?? ""));
        return new Response("{}");
      }),
    );
    const { pi, handlers } = fakePi();
    palinode(pi);
    await handlers.get("session_shutdown")!(
      {},
      { sessionManager: { getEntries: () => entries } },
    );
    const post = posts.find((p) => p.includes("/session-end"));
    expect(post).toBeDefined();
    expect(post).toContain("pi-extension");
    expect(post).toContain("3 messages");
  });

  it("skips trivial sessions and survives a missing sessionManager", async () => {
    const fetchSpy = vi.fn(async () => new Response("{}"));
    vi.stubGlobal("fetch", fetchSpy);
    const { pi, handlers } = fakePi();
    palinode(pi);
    await handlers.get("session_shutdown")!({}, emptyCtx);
    expect(
      fetchSpy.mock.calls.filter((c) => String(c[0]).includes("/session-end")),
    ).toHaveLength(0);
  });
});


describe("capture policy", () => {
  it("does not read session entries without capture opt-in", async () => {
    vi.stubEnv("PALINODE_CAPTURE_ENABLED", "0");
    const { pi, handlers } = fakePi();
    palinode(pi);
    const getEntries = vi.fn(() => { throw new Error("must not read transcript"); });
    await handlers.get("session_shutdown")!({}, { sessionManager: { getEntries } });
    expect(getEntries).not.toHaveBeenCalled();
  });

  it("does not read excluded session entries or inject denied recall", async () => {
    const fetchSpy = vi.fn(async () => Response.json({ allowed: false }));
    vi.stubGlobal("fetch", fetchSpy);
    const { pi, handlers, sent } = fakePi();
    palinode(pi);
    const getEntries = vi.fn(() => { throw new Error("excluded transcript read"); });
    await handlers.get("session_shutdown")!({}, { sessionManager: { getEntries } });
    await handlers.get("session_start")!({}, emptyCtx);
    expect(await handlers.get("before_agent_start")!({ prompt: "FAKE_SECRET_excluded" }, emptyCtx)).toBeUndefined();
    expect(getEntries).not.toHaveBeenCalled();
    expect(sent).toHaveLength(0);
    expect(JSON.stringify(fetchSpy.mock.calls)).not.toContain("FAKE_SECRET_excluded");
  });
});
