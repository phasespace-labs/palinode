/** The Cline binding: lifecycle wiring, and the invariants that must survive
 *  every refactor — recall is injected into the MESSAGE ARRAY (never the
 *  system prompt), it stays byte-stable across requests so the cached prefix
 *  survives, and Palinode trouble never blocks a turn. */
import { describe, expect, it } from "vitest";
import plugin, {
  createPalinodePlugin,
  INJECTED_KIND,
  type ClineMessage,
} from "../src/index.js";

type Calls = Array<{ url: string; body?: unknown; headers?: Record<string, string> }>;

/** Route-matching fetch stub. Records calls; unrouted paths 404. */
function stubFetch(routes: Record<string, unknown>, calls: Calls = []): typeof fetch {
  return (async (url: unknown, init?: { body?: unknown; headers?: Record<string, string> }) => {
    const u = String(url);
    calls.push({ url: u, body: init?.body ? JSON.parse(String(init.body)) : undefined, headers: init?.headers });
    const hit = Object.entries(routes).find(([path]) => u.includes(path));
    if (!hit) return new Response("not found", { status: 404 });
    return new Response(JSON.stringify(hit[1]), { status: 200 });
  }) as typeof fetch;
}

const QUIET = { "/context/prime": {}, "/list": [], "/check-triggers": [], "/search": { results: [] } };
const HIT = {
  ...QUIET,
  "/search": { results: [{ rel_path: "decisions/rollback.md", raw_score: 0.7, snippet: "git revert + reindex" }] },
};

let seq = 0;
function user(text: string): ClineMessage {
  seq += 1;
  return { id: `u${seq}`, role: "user", createdAt: 1000 + seq, content: [{ type: "text", text }] };
}
function assistant(text: string): ClineMessage {
  seq += 1;
  return { id: `a${seq}`, role: "assistant", createdAt: 1000 + seq, content: [{ type: "text", text }] };
}
function toolMsg(): ClineMessage {
  seq += 1;
  return { id: `t${seq}`, role: "tool", createdAt: 1000 + seq, content: [{ type: "tool-result", toolName: "x", output: "y" }] };
}

const PROMPT = "what did we decide about the deploy rollback path for the api?";
const SYSTEM = "You are Cline. Stable system prompt that must never change.";

function request(messages: ClineMessage[], snapshot: Record<string, unknown> = {}) {
  return {
    snapshot: { agentId: "root", parentAgentId: null, runId: "run-1", iteration: 1, messages, ...snapshot },
    request: { systemPrompt: SYSTEM, messages, tools: [] },
  };
}

function make(routes: Record<string, unknown>, calls: Calls = [], extra: Record<string, unknown> = {}) {
  return createPalinodePlugin({ env: {}, fetchFn: stubFetch(routes, calls), ...extra });
}

describe("shape", () => {
  it("is an AgentPlugin with the hooks capability and exactly beforeModel + afterRun", () => {
    expect(plugin.name).toBe("palinode");
    expect(plugin.manifest).toEqual({ capabilities: ["hooks"] });
    expect(Object.keys(plugin.hooks).sort()).toEqual(["afterRun", "beforeModel"]);
  });
});

describe("beforeModel — the prompt-cache invariant", () => {
  it("injects recall as a message after the prompt — and NEVER touches the system prompt", async () => {
    const p = make(HIT);
    const prompt = user(PROMPT);
    const ctx = request([prompt]);
    const result = await p.hooks.beforeModel(ctx);

    expect(result).toBeDefined();
    // The load-bearing assertions. Cline's `AgentBeforeModelResult` cannot even
    // carry a system prompt; if a refactor ever finds a way, per-turn content
    // there would invalidate the provider's cached prefix on every call.
    expect(result).not.toHaveProperty("systemPrompt");
    expect(ctx.request.systemPrompt).toBe(SYSTEM);
    expect(Object.keys(result!)).toEqual(["messages"]);

    const msgs = result!.messages!;
    expect(msgs).toHaveLength(2);
    expect(msgs[0]).toBe(prompt);
    expect(msgs[1]).toMatchObject({ role: "user", metadata: { kind: INJECTED_KIND } });
    const text = (msgs[1].content[0] as { text: string }).text;
    expect(text).toContain("decisions/rollback.md");
    expect(text).toContain("Palinode recall");
  });

  it("returns undefined (request untouched) when nothing is recalled", async () => {
    const p = make(QUIET);
    expect(await p.hooks.beforeModel(request([user(PROMPT)]))).toBeUndefined();
  });

  it("labels a BM25-only hit as rank rather than similarity", async () => {
    const p = make({
      ...QUIET,
      "/search": {
        results: [
          {
            rel_path: "notes/keyword.md",
            score: 1.0,
            raw_score: null,
            snippet: "literal term",
          },
        ],
      },
    });
    const result = await p.hooks.beforeModel(request([user(PROMPT)]));
    const text = (result!.messages![1].content[0] as { text: string }).text;

    expect(text).toContain("[notes/keyword.md] (keyword match, rank 1.00) literal term");
    expect(text).not.toContain("(100%)");
  });

  it("keeps the injected prefix byte-stable across iterations and later turns", async () => {
    const calls: Calls = [];
    const p = make(HIT, calls);
    const u1 = user(PROMPT);

    const r1 = await p.hooks.beforeModel(request([u1]));
    const injected1 = r1!.messages![1];

    // Iteration 2 of the same run: tool loop appended messages; the recall
    // must reappear at the SAME position, identical, without a new query.
    const searchesBefore = calls.filter((c) => c.url.includes("/search")).length;
    const a1 = assistant("calling a tool");
    const t1 = toolMsg();
    const r2 = await p.hooks.beforeModel(request([u1, a1, t1], { iteration: 2 }));
    expect(r2!.messages!.map((m) => m.id)).toEqual([u1.id, injected1.id, a1.id, t1.id]);
    expect(r2!.messages![1]).toEqual(injected1);
    expect(calls.filter((c) => c.url.includes("/search")).length).toBe(searchesBefore);

    // Next user turn (new run): the earlier recall is still replayed in place
    // — the cached prefix through the first turn survives — and the new
    // prompt gets its own recall after it.
    const a2 = assistant("done");
    const u2 = user("and how does that interact with the reindex step afterwards?");
    const r3 = await p.hooks.beforeModel(request([u1, a1, t1, a2, u2], { runId: "run-2", iteration: 1 }));
    const ids = r3!.messages!.map((m) => m.id);
    expect(ids.slice(0, 4)).toEqual([u1.id, injected1.id, a1.id, t1.id]);
    expect(ids.slice(-2)[0]).toBe(u2.id);
    expect(r3!.messages!.at(-1)).toMatchObject({ metadata: { kind: INJECTED_KIND } });
    expect(r3!.messages!.at(-1)!.id).not.toBe(injected1.id);
  });

  it("primes once per session (core digest rides with the first recall) and never again", async () => {
    const calls: Calls = [];
    const p = make(
      { ...HIT, "/list": [{ file: "projects/p.md", name: "P", summary: "core thing" }] },
      calls,
    );
    const r1 = await p.hooks.beforeModel(request([user(PROMPT)]));
    const text = (r1!.messages![1].content[0] as { text: string }).text;
    expect(text).toContain("Palinode memory (session start)");
    expect(text).toContain("[projects/p.md] P — core thing");
    expect(text.indexOf("session start")).toBeLessThan(text.indexOf("Palinode recall"));

    await p.hooks.beforeModel(request([user("second prompt about the same rollback topic")], { runId: "run-2" }));
    expect(calls.filter((c) => c.url.includes("/context/prime"))).toHaveLength(1);
    expect(calls.filter((c) => c.url.includes("/list"))).toHaveLength(1);
  });

  it("skips subagents — recall goes to the root agent only", async () => {
    const calls: Calls = [];
    const p = make(HIT, calls);
    const r = await p.hooks.beforeModel(request([user(PROMPT)], { parentAgentId: "root", agentId: "child" }));
    expect(r).toBeUndefined();
    expect(calls).toHaveLength(0);
  });

  it("ignores its own injected messages when looking for the prompt", async () => {
    const calls: Calls = [];
    const p = make(HIT, calls);
    const u1 = user(PROMPT);
    const r1 = await p.hooks.beforeModel(request([u1]));
    // A host that persisted the injected message must not trigger recall on it.
    const r2 = await p.hooks.beforeModel(request([u1, r1!.messages![1]], { iteration: 2 }));
    expect(calls.filter((c) => c.url.includes("/search"))).toHaveLength(1);
    expect(r2!.messages!.filter((m) => m.metadata?.kind === INJECTED_KIND)).toHaveLength(2);
  });
});

describe("beforeModel — fail-open", () => {
  it("returns undefined (not an error) when the API is down", async () => {
    const p = createPalinodePlugin({
      env: {},
      fetchFn: (async () => {
        throw new Error("connection refused");
      }) as typeof fetch,
    });
    await expect(p.hooks.beforeModel(request([user(PROMPT)]))).resolves.toBeUndefined();
  });

  it("gives up inside the hook deadline when the API hangs, and does not retry that prompt", async () => {
    let hangs = 0;
    const hanging = (async () => {
      hangs += 1;
      await new Promise(() => {}); // never resolves
      return new Response("{}");
    }) as typeof fetch;
    const p = createPalinodePlugin({ env: {}, fetchFn: hanging, hookDeadlineMs: 50 });
    const u1 = user(PROMPT);
    const started = Date.now();
    await expect(p.hooks.beforeModel(request([u1]))).resolves.toBeUndefined();
    expect(Date.now() - started).toBeLessThan(1000);
    const before = hangs;
    await expect(p.hooks.beforeModel(request([u1, assistant("x")], { iteration: 2 }))).resolves.toBeUndefined();
    expect(hangs).toBe(before);
  });

  it("returns undefined on a malformed request rather than throwing", async () => {
    const p = make(HIT);
    const bad = { snapshot: {}, request: { messages: undefined as unknown as ClineMessage[] } };
    await expect(p.hooks.beforeModel(bad)).resolves.toBeUndefined();
  });
});

describe("config surface", () => {
  it("sends Authorization: Bearer on every call when a token is configured, and never otherwise", async () => {
    const calls: Calls = [];
    const p = make(HIT, calls, { token: "sekrit", threshold: 0.6 });
    await p.hooks.beforeModel(request([user(PROMPT)]));
    expect(calls.length).toBeGreaterThan(0);
    expect(calls.every((c) => c.headers?.["Authorization"] === "Bearer sekrit")).toBe(true);
    expect(calls.find((c) => c.url.includes("/search"))?.body).toMatchObject({ threshold: 0.6 });

    const calls2: Calls = [];
    await make(HIT, calls2).hooks.beforeModel(request([user(PROMPT)]));
    expect(calls2.every((c) => c.headers?.["Authorization"] === undefined)).toBe(true);
  });

  it("reads the shared env knobs (URL, token, threshold, profile) when no overrides are given", async () => {
    const calls: Calls = [];
    const p = createPalinodePlugin({
      env: {
        PALINODE_API_URL: "http://memory.example:6340",
        PALINODE_API_TOKEN: "envtok",
        PALINODE_HOOK_RECALL_THRESHOLD: "0.4",
        PALINODE_HOOK_RECALL_PROFILE: "investigation",
      },
      fetchFn: stubFetch(HIT, calls),
    });
    await p.hooks.beforeModel(request([user(PROMPT)]));
    const search = calls.find((c) => c.url.includes("/search"));
    expect(search?.url.startsWith("http://memory.example:6340/")).toBe(true);
    expect(search?.headers?.["Authorization"]).toBe("Bearer envtok");
    expect(search?.body).toMatchObject({ threshold: 0.4, limit: 8 });
    // investigation: no triggers, no priming
    expect(calls.some((c) => c.url.includes("/check-triggers"))).toBe(false);
    expect(calls.some((c) => c.url.includes("/list"))).toBe(false);
  });

  it("recallProfile 'off' makes the plugin inert without unregistering it", async () => {
    const calls: Calls = [];
    const p = make(HIT, calls, { recallProfile: "off" });
    expect(await p.hooks.beforeModel(request([user(PROMPT)]))).toBeUndefined();
    expect(calls.filter((c) => !c.url.includes("/context/prime"))).toHaveLength(0);
  });
});

describe("afterRun — capture floor", () => {
  const convo = (n: number) => {
    const out: ClineMessage[] = [];
    for (let i = 0; i < n; i++) {
      out.push(user(`prompt ${i}: fix the rollback path`));
      out.push(assistant(`answer ${i}`));
    }
    return out;
  };
  const run = (messages: ClineMessage[], snapshot: Record<string, unknown> = {}) => ({
    snapshot: { agentId: "root", parentAgentId: null, runId: "r", iteration: 1, messages, ...snapshot },
    result: { status: "completed", messages },
  });

  it("posts a floor capture once the session crosses the message floor, tagged for Cline", async () => {
    const calls: Calls = [];
    const p = make({ ...QUIET, "/session-end": { status: "ok" } }, calls);
    p.setup!({}, { session: { sessionId: "sess-9" }, workspaceInfo: { rootPath: "/tmp/work/myproj" } });

    await p.hooks.afterRun(run(convo(2)));
    expect(calls.filter((c) => c.url.includes("/session-end"))).toHaveLength(0);

    await p.hooks.afterRun(run(convo(3)));
    const post = calls.find((c) => c.url.includes("/session-end"));
    expect(post?.body).toMatchObject({
      source: "cline-plugin",
      harness: "cline",
      trigger: "run_end",
      project: "myproj",
      session_id: "sess-9",
      cwd: "/tmp/work/myproj",
    });
    expect(String((post?.body as { summary: string }).summary)).toContain("3 messages");
  });

  it("re-captures only when the session doubles — bounded, not per turn", async () => {
    const calls: Calls = [];
    const p = make({ ...QUIET, "/session-end": { status: "ok" } }, calls);
    for (const n of [3, 4, 5, 6, 7, 11, 12, 13]) await p.hooks.afterRun(run(convo(n)));
    const posts = calls.filter((c) => c.url.includes("/session-end"));
    expect(posts.map((c) => /(\d+) messages/.exec(String((c.body as { summary: string }).summary))?.[1])).toEqual([
      "3",
      "6",
      "12",
    ]);
  });

  it("skips subagent runs and is fail-open when the API is down", async () => {
    const calls: Calls = [];
    const p = make({ ...QUIET, "/session-end": { status: "ok" } }, calls);
    await p.hooks.afterRun(run(convo(5), { parentAgentId: "root" }));
    expect(calls).toHaveLength(0);

    const down = createPalinodePlugin({
      env: {},
      fetchFn: (async () => {
        throw new Error("connection refused");
      }) as typeof fetch,
    });
    await expect(down.hooks.afterRun(run(convo(5)))).resolves.toBeUndefined();
  });
});
