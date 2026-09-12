import { describe, expect, it } from "vitest";
import {
  buildCoreDigest,
  buildRecallContext,
  buildSessionCapture,
  configFromEnv,
  postSessionCapture,
  PROFILES,
  restoreMemory,
  unretractMentions,
  userEntries,
  withdrawForgetRequest,
  type FetchFn,
  type PalinodeConfig,
} from "../src/index.js";

const CFG: PalinodeConfig = {
  apiUrl: "http://test:6340",
  recallProfile: "coding",
  maxResults: 3,
  threshold: 0.5,
  triggersOn: true,
  minChars: 12,
  maxChars: 3000,
  timeoutMs: 4000,
  coreMaxFiles: 10,
  coreMaxChars: 4000,
  minMessages: 3,
};

const ORIGIN = { project: "myproj", source: "pi-extension", harness: "pi", trigger: "session_shutdown" };

const PROMPT = "how did we decide to handle the deploy rollback for the api?";

/** Route-matching fetch stub. Records calls; unrouted paths 404. */
function stubFetch(routes: Record<string, unknown>, calls: Array<{ url: string; body?: unknown }> = []): FetchFn {
  return (async (url: unknown, init?: { body?: unknown }) => {
    const u = String(url);
    calls.push({ url: u, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    const hit = Object.entries(routes).find(([path]) => u.includes(path));
    if (!hit) return new Response("not found", { status: 404 });
    return new Response(JSON.stringify(hit[1]), { status: 200 });
  }) as FetchFn;
}

const failingFetch: FetchFn = (async () => {
  throw new Error("connection refused");
}) as FetchFn;

describe("the invariant the core owns", () => {
  it("exports nothing that produces a system prompt — only message bodies and payloads", async () => {
    const mod = await import("../src/index.js");
    for (const [name, value] of Object.entries(mod)) {
      if (typeof value === "function") {
        expect(name.toLowerCase(), `export ${name}`).not.toContain("system");
      }
    }
    const ctx = await buildRecallContext(
      PROMPT,
      CFG,
      stubFetch({ "/check-triggers": [], "/search": { results: [{ rel_path: "a.md", snippet: "x" }] } }),
    );
    // A string, not a request-shaped object: the binding decides which
    // message slot it lands in, and the bindings' suites pin "message".
    expect(typeof ctx).toBe("string");
  });
});

describe("buildRecallContext", () => {
  it("describes cosine, keyword-only, and legacy search hits without treating rank as similarity", async () => {
    const fetchFn = stubFetch({
      "/check-triggers": [],
      "/search": {
        results: [
          // score is the fused rank value (~1.0 for any top hit); raw_score
          // is the cosine the threshold knob filters on. Display must use
          // raw_score so the on-screen number matches the tunable scale.
          { rel_path: "decisions/deploy-rollback.md", score: 1.0, raw_score: 0.62, snippet: "git revert + reindex" },
          { rel_path: "notes/keyword.md", score: 0.98, raw_score: null, snippet: "literal term" },
          { rel_path: "notes/legacy.md", score: 0.75, snippet: "old server" },
        ],
      },
    });
    const ctx = await buildRecallContext(PROMPT, CFG, fetchFn);
    expect(ctx).toContain("[decisions/deploy-rollback.md] (62% match) git revert + reindex");
    expect(ctx).toContain("[notes/keyword.md] (keyword match, rank 0.98) literal term");
    expect(ctx).toContain("[notes/legacy.md] (rank 0.75) old server");
    expect(ctx).not.toContain("(100%)");
    expect(ctx).not.toContain("(98%)");
    expect(ctx).not.toContain("(75%)");
    expect(ctx).toContain("Related memories");
    expect(ctx).toContain("may be stale");
  });

  it("injects fired-trigger content via /read", async () => {
    const fetchFn = stubFetch({
      "/check-triggers": [{ id: "t1", memory_file: "decisions/deploy-rollback.md", score: 0.9 }],
      "/read": { content: "Full rollback decision body." },
      "/search": { results: [] },
    });
    const ctx = await buildRecallContext(PROMPT, CFG, fetchFn);
    expect(ctx).toContain("Trigger fired: decisions/deploy-rollback.md");
    expect(ctx).toContain("Full rollback decision body.");
  });

  it("sends the strict defaults in the search payload", async () => {
    const calls: Array<{ url: string; body?: unknown }> = [];
    const fetchFn = stubFetch({ "/check-triggers": [], "/search": { results: [] } }, calls);
    await buildRecallContext(PROMPT, CFG, fetchFn);
    const search = calls.find((c) => c.url.includes("/search"));
    expect(search?.body).toMatchObject({ limit: 3, threshold: 0.5, max_chars: 300 });
  });

  it("returns null when nothing is recalled", async () => {
    const fetchFn = stubFetch({ "/check-triggers": [], "/search": { results: [] } });
    expect(await buildRecallContext(PROMPT, CFG, fetchFn)).toBeNull();
  });

  it("returns null (never throws) when the API is down", async () => {
    expect(await buildRecallContext(PROMPT, CFG, failingFetch)).toBeNull();
  });

  it("skips trivial prompts before any network call", async () => {
    const calls: Array<{ url: string }> = [];
    const fetchFn = stubFetch({}, calls);
    expect(await buildRecallContext("ok", CFG, fetchFn)).toBeNull();
    expect(calls).toHaveLength(0);
  });

  it("respects channel switches", async () => {
    const calls: Array<{ url: string }> = [];
    const fetchFn = stubFetch({ "/check-triggers": [], "/search": { results: [] } }, calls);
    await buildRecallContext(PROMPT, { ...CFG, maxResults: 0 }, fetchFn);
    expect(calls.some((c) => c.url.includes("/search"))).toBe(false);
    calls.length = 0;
    await buildRecallContext(PROMPT, { ...CFG, triggersOn: false }, fetchFn);
    expect(calls.some((c) => c.url.includes("check-triggers"))).toBe(false);
  });

  it("bounds total context at maxChars", async () => {
    const fetchFn = stubFetch({
      "/check-triggers": [{ memory_file: "a.md" }],
      "/read": { content: "x".repeat(50_000) },
      "/search": { results: [] },
    });
    const ctx = await buildRecallContext(PROMPT, { ...CFG, maxChars: 500 }, fetchFn);
    expect(ctx).not.toBeNull();
    expect(ctx!.length).toBeLessThanOrEqual(500);
  });

  it("caps fired triggers at two files", async () => {
    const calls: Array<{ url: string }> = [];
    const fetchFn = stubFetch(
      {
        "/check-triggers": [
          { memory_file: "a.md" },
          { memory_file: "b.md" },
          { memory_file: "c.md" },
        ],
        "/read": { content: "body" },
        "/search": { results: [] },
      },
      calls,
    );
    await buildRecallContext(PROMPT, CFG, fetchFn);
    expect(calls.filter((c) => c.url.includes("/read")).length).toBe(2);
  });
});

describe("auth", () => {
  it("sends the bearer token when configured, and no header otherwise", async () => {
    let seenAuth: string | null | undefined;
    const fetchFn = (async (_url: unknown, init?: { headers?: Record<string, string> }) => {
      seenAuth = init?.headers?.["Authorization"];
      return new Response("[]", { status: 200 });
    }) as FetchFn;

    await buildRecallContext(PROMPT, { ...CFG, maxResults: 0, token: "sekrit" }, fetchFn);
    expect(seenAuth).toBe("Bearer sekrit");

    await buildRecallContext(PROMPT, { ...CFG, maxResults: 0 }, fetchFn);
    expect(seenAuth).toBeUndefined();
  });

  it("carries the bearer on every endpoint, including the capture POST", async () => {
    const auths: string[] = [];
    const fetchFn = (async (_url: unknown, init?: { headers?: Record<string, string> }) => {
      auths.push(init?.headers?.["Authorization"] ?? "");
      return new Response("{}", { status: 200 });
    }) as FetchFn;
    const cfg = { ...CFG, token: "t0k" };
    await buildCoreDigest(cfg, fetchFn, "/tmp/p", "s");
    await postSessionCapture({ summary: "s", project: "p", source: "x", harness: "h", trigger: "t", decisions: [], blockers: [] }, cfg, fetchFn);
    expect(auths.length).toBeGreaterThan(0);
    expect(auths.every((a) => a === "Bearer t0k")).toBe(true);
  });
});

describe("buildCoreDigest", () => {
  it("primes the server and renders a bounded core digest", async () => {
    const calls: Array<{ url: string }> = [];
    const fetchFn = stubFetch(
      {
        "/context/prime": {},
        "/list": [
          { file: "projects/palinode.md", name: "Palinode", summary: "memory system" },
        ],
      },
      calls,
    );
    const digest = await buildCoreDigest(CFG, fetchFn, "/tmp/proj", "s1");
    expect(calls.some((c) => c.url.includes("/context/prime"))).toBe(true);
    expect(digest).toContain("[projects/palinode.md] Palinode — memory system");
    expect(digest).toContain("session start");
  });

  it("returns null with no core memories, and skips listing when disabled", async () => {
    const calls: Array<{ url: string }> = [];
    const fetchFn = stubFetch({ "/context/prime": {}, "/list": [] }, calls);
    expect(await buildCoreDigest(CFG, fetchFn)).toBeNull();
    calls.length = 0;
    expect(await buildCoreDigest({ ...CFG, coreMaxFiles: 0 }, fetchFn)).toBeNull();
    expect(calls.some((c) => c.url.includes("/list"))).toBe(false);
  });

  it("is fail-open when the API is down", async () => {
    expect(await buildCoreDigest(CFG, failingFetch)).toBeNull();
  });
});

describe("session capture", () => {
  const piEntries = (n: number) =>
    Array.from({ length: n }, (_, i) => ({
      type: "message",
      message: { role: "user", content: `prompt ${i}: fix the rollback path` },
    }));

  const clineEntries = (n: number) =>
    Array.from({ length: n }, (_, i) => ({
      id: `m${i}`,
      role: "user",
      content: [{ type: "text", text: `prompt ${i}: fix the rollback path` }],
    }));

  it("derives the floor payload from Pi-shaped entries", () => {
    const payload = buildSessionCapture(piEntries(4), CFG, ORIGIN);
    expect(payload).not.toBeNull();
    expect(payload!.summary).toContain("4 messages");
    expect(payload!.summary).toContain("Topic: prompt 0: fix the rollback path");
    expect(payload!.summary).toContain("Latest: prompt 3: fix the rollback path");
    expect(payload!.source).toBe("pi-extension");
    expect(payload!.harness).toBe("pi");
    expect(payload!.trigger).toBe("session_shutdown");
    expect(payload!.project).toBe("myproj");
    expect(payload!).not.toHaveProperty("session_id");
  });

  it("reads Cline-shaped entries (top-level role, content parts) the same way", () => {
    const payload = buildSessionCapture(clineEntries(3), CFG, {
      ...ORIGIN,
      source: "cline-plugin",
      harness: "cline",
      trigger: "run_end",
      sessionId: "sess-1",
      cwd: "/tmp/proj",
    });
    expect(payload).not.toBeNull();
    expect(payload!.summary).toContain("cline run_end, 3 messages");
    expect(payload!.session_id).toBe("sess-1");
    expect(payload!.cwd).toBe("/tmp/proj");
  });

  it("counts only user entries that carry text", () => {
    const mixed = [
      ...clineEntries(2),
      { id: "a", role: "assistant", content: [{ type: "text", text: "sure" }] },
      { id: "t", role: "tool", content: [{ type: "tool-result", toolName: "x", output: "y" }] },
      { id: "img", role: "user", content: [{ type: "image", image: "..." }] },
    ];
    expect(userEntries(mixed)).toHaveLength(2);
    expect(buildSessionCapture(mixed, CFG, ORIGIN)).toBeNull();
  });

  it("skips trivial sessions below the message floor", () => {
    expect(buildSessionCapture(piEntries(2), CFG, ORIGIN)).toBeNull();
  });

  it("posts fail-open", async () => {
    const payload = buildSessionCapture(piEntries(3), CFG, ORIGIN)!;
    expect(await postSessionCapture(payload, CFG, failingFetch)).toBe(false);
    const okFetch = stubFetch({ "/session-end": { status: "ok" } });
    expect(await postSessionCapture(payload, CFG, okFetch)).toBe(true);
  });
});

describe("configFromEnv", () => {
  it("shares the Claude Code hook env vars — one set of knobs, every harness", () => {
    const cfg = configFromEnv({
      PALINODE_API_URL: "http://remote:6340",
      PALINODE_API_TOKEN: "t",
      PALINODE_HOOK_RECALL_MAX_RESULTS: "5",
      PALINODE_HOOK_RECALL_THRESHOLD: "0.8",
      PALINODE_HOOK_RECALL_TRIGGERS: "0",
      PALINODE_HOOK_RECALL_MIN_CHARS: "20",
      PALINODE_HOOK_RECALL_MAX_CHARS: "1000",
      PALINODE_HOOK_RECALL_TIMEOUT: "2",
      PALINODE_HOOK_INJECT_MAX_FILES: "5",
      PALINODE_HOOK_INJECT_MAX_CHARS: "2000",
      PALINODE_HOOK_MIN_MESSAGES: "1",
    });
    expect(cfg).toEqual({
      apiUrl: "http://remote:6340",
      token: "t",
      recallProfile: "coding",
      maxResults: 5,
      threshold: 0.8,
      triggersOn: false,
      minChars: 20,
      maxChars: 1000,
      timeoutMs: 2000,
      coreMaxFiles: 5,
      coreMaxChars: 2000,
      minMessages: 1,
    });
  });

  it("defaults match the Claude Code hook defaults", () => {
    const cfg = configFromEnv({});
    expect(cfg.apiUrl).toBe("http://localhost:6340");
    expect(cfg.recallProfile).toBe("coding");
    expect(cfg.maxResults).toBe(3);
    // Calibrated default: 0.5 = 98% measured recall; 0.7+ was near-dead.
    expect(cfg.threshold).toBe(0.5);
    expect(cfg.triggersOn).toBe(true);
    expect(cfg.minChars).toBe(12);
    expect(cfg.maxChars).toBe(3000);
    expect(cfg.timeoutMs).toBe(4000);
    expect(cfg.coreMaxFiles).toBe(10);
    expect(cfg.minMessages).toBe(3);
  });

  it("applies a recall profile's channel knobs, with explicit env winning over the profile", () => {
    const mon = configFromEnv({ PALINODE_HOOK_RECALL_PROFILE: "monitoring" });
    expect(mon.recallProfile).toBe("monitoring");
    expect(mon).toMatchObject(PROFILES.monitoring);

    const tuned = configFromEnv({
      PALINODE_HOOK_RECALL_PROFILE: "monitoring",
      PALINODE_HOOK_RECALL_MAX_RESULTS: "2",
    });
    expect(tuned.maxResults).toBe(2);
    expect(tuned.triggersOn).toBe(true);
    expect(tuned.coreMaxFiles).toBe(0);

    expect(configFromEnv({ PALINODE_HOOK_RECALL_PROFILE: "off" })).toMatchObject({
      maxResults: 0,
      triggersOn: false,
      coreMaxFiles: 0,
    });
  });

  it("falls back to coding on an unknown profile name", () => {
    expect(configFromEnv({ PALINODE_HOOK_RECALL_PROFILE: "bogus" }).recallProfile).toBe("coding");
  });

  it("lets a harness's own config override the env, and undefined overrides are ignored", () => {
    const cfg = configFromEnv(
      { PALINODE_API_URL: "http://env:6340", PALINODE_HOOK_RECALL_THRESHOLD: "0.6" },
      { apiUrl: "http://override:6340", token: "abc", threshold: undefined, recallProfile: "writing" },
    );
    expect(cfg.apiUrl).toBe("http://override:6340");
    expect(cfg.token).toBe("abc");
    expect(cfg.threshold).toBe(0.6);
    expect(cfg.recallProfile).toBe("writing");
    expect(cfg.maxResults).toBe(0);
    expect(cfg.coreMaxFiles).toBe(10);
  });
});

describe("reversal client (restore / unretract / forget-withdraw)", () => {
  it("restoreMemory posts the canonical params to /restore and returns the result", async () => {
    const calls: Array<{ url: string; body?: unknown }> = [];
    const fetchFn = stubFetch(
      { "/restore": { file: "insights/x.md", status: "active", restored_from: "archived", chunks_updated: 2 } },
      calls,
    );
    const out = await restoreMemory("insights/x.md", CFG, fetchFn, "wrongly retired");
    expect(out).toMatchObject({ file: "insights/x.md", status: "active", restored_from: "archived" });
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe("http://test:6340/restore");
    expect(calls[0].body).toEqual({ file_path: "insights/x.md", reason: "wrongly retired" });
  });

  it("restoreMemory omits reason when not given", async () => {
    const calls: Array<{ url: string; body?: unknown }> = [];
    await restoreMemory("insights/x.md", CFG, stubFetch({ "/restore": { file: "insights/x.md", status: "not_archived" } }, calls));
    expect(calls[0].body).toEqual({ file_path: "insights/x.md" });
  });

  it("unretractMentions posts file_path + pref to /unretract", async () => {
    const calls: Array<{ url: string; body?: unknown }> = [];
    const out = await unretractMentions(
      "projects/closeout.md",
      "I know Wilhelmina Cragg",
      CFG,
      stubFetch({ "/unretract": { file: "projects/closeout.md", status: "unretracted", mentions: 2 } }, calls),
    );
    expect(out).toMatchObject({ status: "unretracted", mentions: 2 });
    expect(calls[0].url).toBe("http://test:6340/unretract");
    expect(calls[0].body).toEqual({ file_path: "projects/closeout.md", pref: "I know Wilhelmina Cragg" });
  });

  it("withdrawForgetRequest posts the request path to /forget-withdraw", async () => {
    const calls: Array<{ url: string; body?: unknown }> = [];
    const out = await withdrawForgetRequest(
      "insights/forget-sneakers.md",
      CFG,
      stubFetch(
        {
          "/forget-withdraw": {
            file: "insights/forget-sneakers.md",
            status: "withdrawn",
            pref: "I collect vintage sneakers",
            restored: ["insights/pref-sneakers.md"],
            unretracted: [],
            requests_archived: ["insights/forget-sneakers.md"],
          },
        },
        calls,
      ),
    );
    expect(out?.restored).toEqual(["insights/pref-sneakers.md"]);
    expect(calls[0].url).toBe("http://test:6340/forget-withdraw");
    expect(calls[0].body).toEqual({ file_path: "insights/forget-sneakers.md" });
  });

  it("all three fail open: API down or HTTP error resolves to null, never throws", async () => {
    expect(await restoreMemory("insights/x.md", CFG, failingFetch)).toBeNull();
    expect(await unretractMentions("insights/x.md", "p", CFG, failingFetch)).toBeNull();
    expect(await withdrawForgetRequest("insights/x.md", CFG, failingFetch)).toBeNull();
    const notFound = stubFetch({}); // unrouted → 404
    expect(await restoreMemory("insights/x.md", CFG, notFound)).toBeNull();
  });
});
