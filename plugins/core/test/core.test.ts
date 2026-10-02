import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, realpathSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { describe, expect, it } from "vitest";
import {
  archiveMemory,
  buildCoreDigest,
  buildRecallContext,
  buildSessionCapture,
  clientCwd,
  RESOLUTION_DEADLINE_MARKER,
  trimToUnitBoundary,
  configFromEnv,
  postSessionCapture,
  PROFILES,
  restoreMemory,
  unretractMentions,
  userEntries,
  withdrawForgetRequest,
  type FetchFn,
  type PalinodeConfig,
  FRAME,
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
  captureOn: true,
  // The suites below this line pin the plain search channel — the behaviour a
  // deadline falls back to — so they run with resolution off. The shipped
  // default is ON; `configFromEnv` pins that, and the bounded-resolution
  // suite runs with it enabled.
  resolveOn: false,
  resolveDeadlineMs: 250,
};

const ORIGIN = { cwd: process.cwd(), project: "myproj", source: "pi-extension", harness: "pi", trigger: "session_shutdown" };

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

  it("filters legacy cross_refs on trigger reads in the client's project", async () => {
    const fetchFn = (async (input: unknown) => {
      const url = new URL(String(input));
      if (url.pathname === "/controls/check") return Response.json({ project: "home" });
      if (url.pathname === "/check-triggers") return Response.json([
        { memory_file: "insights/global-rule.md" },
      ]);
      if (url.pathname === "/read") return Response.json({
        content: `---\ncross_refs: [${url.searchParams.get("project") === "home"
          ? "decisions/home-link" : "decisions/foreign-link"}]\n---\nGlobal body`,
      });
      return Response.json({ results: [] });
    }) as FetchFn;
    const ctx = await buildRecallContext(PROMPT, CFG, fetchFn, "/client/workspace");
    expect(ctx).toContain("decisions/home-link");
    expect(ctx).not.toContain("decisions/foreign-link");
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

describe("bounded resolution (the per-turn consuming hook)", () => {
  const RCFG: PalinodeConfig = { ...CFG, resolveOn: true };

  /** The three scenarios exactly as the Python suite pins them. */
  const BUNDLES = JSON.parse(
    readFileSync(
      new URL("../../../tests/fixtures/resolve_bundles.json", import.meta.url),
      "utf-8",
    ),
  ) as Record<string, Record<string, unknown>>;

  it("injects the resolved bundle, and a scripted consumer picks the successor", async () => {
    // The A → B replacement, resolved: a fresh session is told B, with the
    // evidence — never A's retired wording.
    const calls: Array<{ url: string; body?: unknown }> = [];
    const ctx = await buildRecallContext(
      "which endpoint does production serve from?",
      RCFG,
      stubFetch({ "/check-triggers": [], "/resolve": BUNDLES.current }, calls),
    );
    expect(calls.some((c) => c.url.includes("/resolve"))).toBe(true);
    expect(calls.some((c) => c.url.includes("/search"))).toBe(false);
    expect(ctx).toContain("decisions/endpoint-v2");
    expect(ctx).toContain("Production serves traffic from endpoint bravo.");
    expect(ctx).not.toContain("alpha");
    expect(ctx).not.toContain(RESOLUTION_DEADLINE_MARKER);

    // A scripted consumer — the crudest possible reader of the payload —
    // picks B, not A: the answer is the line under "Current".
    const current = ctx!
      .split("\n")
      .slice(ctx!.split("\n").indexOf("Current (1):") + 1)
      .find((line) => line.startsWith("- ["));
    expect(current).toContain("decisions/endpoint-v2");
  });

  it("budgets the bundle at the room actually left, frame subtracted", async () => {
    let seenTimeout: number | undefined;
    const fetchFn = (async (url: unknown, init?: { body?: unknown; signal?: AbortSignal }) => {
      if (String(url).includes("/resolve")) {
        // AbortSignal.timeout(ms) is opaque; the deadline is asserted through
        // the abort behaviour in the deadline test below. Here: the payload.
        seenTimeout = 1;
        return new Response(JSON.stringify(BUNDLES.current), { status: 200 });
      }
      return new Response("[]", { status: 200 });
    }) as FetchFn;
    const calls: Array<{ url: string; body?: unknown }> = [];
    await buildRecallContext(PROMPT, RCFG, (async (url: unknown, init?: { body?: unknown }) => {
      calls.push({ url: String(url), body: init?.body ? JSON.parse(String(init.body)) : undefined });
      return fetchFn(url as string, init as RequestInit);
    }) as FetchFn);
    expect(seenTimeout).toBe(1);
    const resolveCall = calls.find((c) => c.url.includes("/resolve"));
    const body = resolveCall?.body as { max_items: number; max_chars: number };
    expect(body.max_items).toBe(3);
    // The injection cap minus the fixed preamble: asking for the whole cap is
    // how a conflict the server packed whole gets sliced in half on arrival.
    expect(body.max_chars).toBeLessThan(CFG.maxChars);
    expect(body.max_chars).toBeGreaterThan(CFG.maxChars - 400);
  });

  it("carries the client's cwd to resolution, and nothing when there is none", async () => {
    const bodies: Array<Record<string, unknown>> = [];
    const fetchFn = (async (url: unknown, init?: { body?: unknown }) => {
      if (String(url).includes("/resolve")) bodies.push(JSON.parse(String(init?.body)));
      return new Response(JSON.stringify(BUNDLES.current), { status: 200 });
    }) as FetchFn;
    await buildRecallContext(PROMPT, RCFG, fetchFn, "/work/quillon-client");
    await buildRecallContext(PROMPT, RCFG, fetchFn);
    expect(bodies[0].cwd).toBe("/work/quillon-client");
    expect("cwd" in bodies[1]).toBe(false);
  });

  it("says nothing when the injection cap leaves no room for an honest answer", async () => {
    const calls: Array<{ url: string }> = [];
    const ctx = await buildRecallContext(
      PROMPT,
      { ...RCFG, maxChars: 200, triggersOn: false },
      stubFetch({ "/resolve": BUNDLES.conflict, "/search": { results: [] } }, calls),
    );
    expect(ctx).toBeNull();
    // Not even the fallback: one side of a conflict rendered as a plain hit is
    // the failure bounded resolution exists to prevent.
    expect(calls.some((c) => c.url.includes("/resolve"))).toBe(false);
    expect(calls.some((c) => c.url.includes("/search"))).toBe(false);
  });

  it("trims the trigger section, never the resolved bundle, to fit the cap", async () => {
    const ctx = await buildRecallContext(
      PROMPT,
      { ...RCFG, maxChars: 1200 },
      stubFetch({
        "/check-triggers": [{ memory_file: "a.md" }],
        "/read": { content: "x".repeat(50_000) },
        "/resolve": BUNDLES.conflict,
      }),
    );
    expect(ctx!.length).toBeLessThanOrEqual(1200);
    // Both sides of the conflict survived the trim.
    expect(ctx).toContain("insights/region-a");
    expect(ctx).toContain("insights/region-b");
  });

  it("keeps both sides of an unresolved conflict in the injected payload", async () => {
    const ctx = await buildRecallContext(
      "where does the cache cluster run?",
      RCFG,
      stubFetch({ "/check-triggers": [], "/resolve": BUNDLES.conflict }),
    );
    expect(ctx).toContain("insights/region-a");
    expect(ctx).toContain("insights/region-b");
    expect(ctx).toContain("Contested");
    expect(ctx).toContain("no winner");
  });

  it("passes an omitted conflict through as still contested, never as settled", async () => {
    // A tight output budget on the server side: the group did not fit, so the
    // bundle reports it by ref. The injected payload must carry that through.
    const tight = {
      ...BUNDLES.conflict,
      conflicts: [],
      omitted_conflicts: 1,
      omitted_conflict_refs: [["insights/region-a", "insights/region-b"]],
      coverage: { status: "partial", reasons: ["budget_exhausted:conflicts"] },
      text:
        "### Resolved from memory (current state)\n\n" +
        "Still contested, omitted for budget (1): insights/region-a ↔ insights/region-b\n\n" +
        "Coverage: partial (budget_exhausted:conflicts)",
      selected: [],
    };
    const ctx = await buildRecallContext(
      "where does the cache cluster run?",
      RCFG,
      stubFetch({ "/check-triggers": [], "/resolve": tight }),
    );
    expect(ctx).toContain("Still contested");
    expect(ctx).toContain("budget_exhausted:conflicts");
    expect(ctx).toContain("insights/region-a");
    expect(ctx).toContain("insights/region-b");
  });

  it("leads unframed fallback memory with the authority frame", async () => {
    const hits = [
      { rel_path: "decisions/deploy-rollback.md", score: 1.0, raw_score: 0.62, snippet: "git revert" },
    ];
    const fetchFn = (async (url: unknown) => {
      if (String(url).includes("/resolve")) throw new DOMException("TimeoutError", "TimeoutError");
      if (String(url).includes("/search")) return new Response(JSON.stringify({ results: hits }), { status: 200 });
      return new Response("[]", { status: 200 });
    }) as FetchFn;
    const ctx = (await buildRecallContext(PROMPT, RCFG, fetchFn))!;
    expect(ctx).toContain(FRAME);
    expect(ctx.indexOf(FRAME)).toBeLessThan(ctx.indexOf("git revert"));
  });

  it("does not add a second frame to a bundle that carries its own", async () => {
    const ctx = (await buildRecallContext(
      "which region does the cache cluster run in?",
      RCFG,
      stubFetch({ "/check-triggers": [], "/resolve": BUNDLES.conflict }),
    ))!;
    const line = FRAME.trimEnd();
    expect(BUNDLES.conflict.text).toContain(line);
    expect(ctx.split(line).length - 1).toBe(1);
  });

  it("carries an explicit unknown rather than an older value", async () => {
    const ctx = await buildRecallContext(
      "what is the pipeline throughput ceiling?",
      RCFG,
      stubFetch({ "/check-triggers": [], "/resolve": BUNDLES.unknown }),
    );
    expect(ctx).toContain("Unknown");
    expect(ctx).toContain("support_withdrawn");
    expect(ctx).not.toContain("4000 rps");
  });

  it("falls back to today's payload on deadline — with the marker, never silently", async () => {
    const hits = [
      { rel_path: "decisions/deploy-rollback.md", score: 1.0, raw_score: 0.62, snippet: "git revert" },
    ];
    // /resolve aborts (the deadline); /search answers as it always has.
    const fetchFn = (async (url: unknown) => {
      if (String(url).includes("/resolve")) throw new DOMException("TimeoutError", "TimeoutError");
      if (String(url).includes("/search")) return new Response(JSON.stringify({ results: hits }), { status: 200 });
      return new Response("[]", { status: 200 });
    }) as FetchFn;

    const ctx = await buildRecallContext(PROMPT, RCFG, fetchFn);
    expect(ctx).toContain(RESOLUTION_DEADLINE_MARKER);
    expect(ctx).toContain("Related memories");
    expect(ctx).toContain("[decisions/deploy-rollback.md] (62% match) git revert");

    // The fallback body is byte-identical to the pre-resolution payload: the
    // marker is added, nothing else moves.
    const before = await buildRecallContext(PROMPT, CFG, fetchFn);
    expect(ctx!.replace(`${RESOLUTION_DEADLINE_MARKER}\n`, "")).toBe(before);
  });

  it("scopes the deadline fallback to the client's project", async () => {
    // /resolve scopes itself through `cwd` server-side; the fallback /search
    // used to send none at all — `{query, limit, threshold, max_chars}` — so
    // a missed deadline delivered what the bundle path would have withheld
    // for the very same client. `/search` honours scope only via `context`.
    const hits = [
      { rel_path: "decisions/deploy-rollback.md", score: 1.0, raw_score: 0.62, snippet: "git revert" },
    ];
    const calls: Array<{ url: string; body?: unknown }> = [];
    const fetchFn = (async (url: unknown, init?: { body?: unknown }) => {
      const u = String(url);
      calls.push({ url: u, body: init?.body ? JSON.parse(String(init.body)) : undefined });
      if (u.includes("/resolve")) throw new DOMException("TimeoutError", "TimeoutError");
      if (u.includes("/controls/check")) {
        return new Response(JSON.stringify({ allowed: true, project: "otherproj" }), { status: 200 });
      }
      if (u.includes("/search")) return new Response(JSON.stringify({ results: hits }), { status: 200 });
      return new Response("[]", { status: 200 });
    }) as FetchFn;

    const ctx = await buildRecallContext(PROMPT, RCFG, fetchFn, "/repo/otherproj");
    expect(ctx).toContain("Related memories");
    const search = calls.find((c) => c.url.includes("/search"));
    expect((search?.body as { context?: unknown[] } | undefined)?.context).toEqual(["project/otherproj"]);
  });

  it("sends no scope on the fallback when the client has no resolvable project", async () => {
    const hits = [{ rel_path: "notes/a.md", score: 1.0, raw_score: 0.6, snippet: "x" }];
    const calls: Array<{ url: string; body?: unknown }> = [];
    const fetchFn = (async (url: unknown, init?: { body?: unknown }) => {
      const u = String(url);
      calls.push({ url: u, body: init?.body ? JSON.parse(String(init.body)) : undefined });
      if (u.includes("/resolve")) throw new DOMException("TimeoutError", "TimeoutError");
      if (u.includes("/controls/check")) {
        return new Response(JSON.stringify({ allowed: true, project: null }), { status: 200 });
      }
      if (u.includes("/search")) return new Response(JSON.stringify({ results: hits }), { status: 200 });
      return new Response("[]", { status: 200 });
    }) as FetchFn;

    await buildRecallContext(PROMPT, RCFG, fetchFn, "/repo/unscoped");
    const search = calls.find((c) => c.url.includes("/search"));
    expect(search?.body).not.toHaveProperty("context");
  });

  it("says nothing when the deadline passes and search recalls nothing either", async () => {
    const fetchFn = (async (url: unknown) => {
      if (String(url).includes("/resolve")) throw new Error("deadline");
      return new Response(JSON.stringify({ results: [] }), { status: 200 });
    }) as FetchFn;
    expect(await buildRecallContext(PROMPT, RCFG, fetchFn)).toBeNull();
  });

  it("says nothing when the bundle resolves to nothing", async () => {
    const empty = {
      selected: [], conflicts: [], replaced: [], insufficient: [],
      omitted_conflicts: 0, coverage: { status: "complete", reasons: [] },
      receipt_ref: null,
      text: "### Resolved from memory (current state)\n\nNothing in memory answers this.",
    };
    const ctx = await buildRecallContext(
      PROMPT, RCFG, stubFetch({ "/check-triggers": [], "/resolve": empty }),
    );
    expect(ctx).toBeNull();
  });

  it("delivers an empty scoped bundle that says other projects were withheld", async () => {
    const line =
      "2 records from other projects withheld (scope: project/alpha). They are about other projects, not this one.";
    const isolated = {
      selected: [], conflicts: [], replaced: [], insufficient: [],
      omitted_conflicts: 0, other_projects_withheld: 2,
      coverage: { status: "complete", reasons: [] }, receipt_ref: null,
      text: `### Resolved from memory (current state)\n\nNothing in memory answers this.\n${line}`,
    };
    const ctx = await buildRecallContext(
      PROMPT, RCFG, stubFetch({ "/check-triggers": [], "/resolve": isolated }),
    );
    expect(ctx).toContain(line);
  });

  it("takes the plain search channel when resolution is switched off", async () => {
    const calls: Array<{ url: string }> = [];
    await buildRecallContext(
      PROMPT,
      { ...RCFG, resolveOn: false },
      stubFetch({ "/check-triggers": [], "/search": { results: [] } }, calls),
    );
    expect(calls.some((c) => c.url.includes("/resolve"))).toBe(false);
    expect(calls.some((c) => c.url.includes("/search"))).toBe(true);
  });

  it("never calls /resolve when the memory channel is disabled", async () => {
    const calls: Array<{ url: string }> = [];
    await buildRecallContext(
      PROMPT, { ...RCFG, maxResults: 0 }, stubFetch({ "/check-triggers": [] }, calls),
    );
    expect(calls.some((c) => c.url.includes("/resolve"))).toBe(false);
  });

  it("defaults to on, at a 250 ms deadline, and is switchable from the env", () => {
    expect(configFromEnv({}).resolveOn).toBe(true);
    expect(configFromEnv({}).resolveDeadlineMs).toBe(250);
    expect(configFromEnv({ PALINODE_HOOK_RESOLVE: "0" }).resolveOn).toBe(false);
    expect(configFromEnv({ PALINODE_HOOK_RESOLVE_DEADLINE: "600" }).resolveDeadlineMs).toBe(600);
  });

  it("session start does not route through resolution", async () => {
    const calls: Array<{ url: string }> = [];
    await buildCoreDigest(
      RCFG,
      stubFetch({ "/context/prime": {}, "/list": [{ file: "a.md", name: "A" }] }, calls),
    );
    expect(calls.some((c) => c.url.includes("/resolve"))).toBe(false);
  });
});

describe("the final injected string is never cut inside a unit", () => {
  const RCFG: PalinodeConfig = { ...CFG, resolveOn: true };

  /** The same three scenarios the Python suite pins, as the server sends them. */
  const BUNDLES = JSON.parse(
    readFileSync(
      new URL("../../../tests/fixtures/resolve_bundles.json", import.meta.url),
      "utf-8",
    ),
  ) as Record<string, { text: string }>;

  /** A trigger body long enough that the cap must bite somewhere. */
  const LONG_TRIGGER = Array.from({ length: 40 }, (_, i) => `- [notes/n${i}.md] line ${i}`).join("\n");

  /** The payload as the model actually receives it: one string, after every
   *  formatting step. Asserting on anything earlier tests a payload nobody
   *  gets. */
  async function injected(scenario: string, maxChars: number): Promise<string | null> {
    return buildRecallContext(
      PROMPT,
      { ...RCFG, maxChars },
      stubFetch({
        "/check-triggers": [{ memory_file: "a.md" }],
        "/read": { content: LONG_TRIGGER },
        "/resolve": BUNDLES[scenario],
      }),
    );
  }

  for (const cap of [900, 1100, 1400, 2000]) {
    it(`keeps a contested bundle whole or named at ${cap} chars`, async () => {
      const ctx = await injected("conflict", cap);
      if (ctx === null) return; // below the floor: silence, pinned elsewhere
      if (ctx.includes("insights/region-a") || ctx.includes("insights/region-b")) {
        // One side is never alone — whichever arrived, the other did too.
        expect(ctx).toContain("insights/region-a");
        expect(ctx).toContain("insights/region-b");
      }
      // Whatever was cut, it was cut between lines.
      expect(ctx.endsWith("\n") || !ctx.endsWith(" ")).toBe(true);
      for (const line of BUNDLES.conflict.text.split("\n")) {
        if (line.trim() === "" || !ctx.includes(line.slice(0, 12))) continue;
        expect(ctx).toContain(line);
      }
    });
  }

  it("keeps the resolved answer's qualifiers attached to it", async () => {
    const ctx = await injected("current", 1100);
    expect(ctx).not.toBeNull();
    // The row and the indented lines that qualify it travel together.
    expect(ctx).toContain("decisions/endpoint-v2");
    expect(ctx).toContain("    qualifiers: epistemic:fact");
    expect(ctx).toContain("Coverage: complete");
  });

  it("keeps an explicit unknown explicit", async () => {
    const ctx = await injected("unknown", 1100);
    expect(ctx).toContain("insights/throughput");
    expect(ctx).toContain("support_withdrawn");
  });

  const CONTESTED_PAYLOAD = [
    "- [core/a.md] A — always true",
    "- [core/b.md] B ⚠ contradicts: core/c.md",
    "- [core/c.md] C ⚠ contradicts: core/b.md",
  ].join("\n");

  it("drops a contested block rather than halving it, and says where it went", () => {
    const trimmed = trimToUnitBoundary(CONTESTED_PAYLOAD, 100);
    expect(trimmed).toContain("- [core/a.md]");
    // Neither side is rendered, because one of them could not be: a kept side
    // beside a dropped one is the half-conflict this rule exists to prevent.
    expect(trimmed).not.toContain("⚠ contradicts");
    // Both are named instead, in the server's own wording.
    expect(trimmed).toContain("⚠ 2 conflicts omitted for budget");
    expect(trimmed).toContain("core/b.md");
    expect(trimmed).toContain("core/c.md");
    expect(trimmed.length).toBeLessThanOrEqual(100);
  });

  it("evicts an ordinary row to make room for the contested stub", () => {
    // "there is a conflict here, here is where" outranks one more plain row —
    // the same eviction rule the Python packer applies.
    const trimmed = trimToUnitBoundary(CONTESTED_PAYLOAD, 60);
    expect(trimmed).toBe("⚠ 2 conflicts omitted for budget — see core/b.md, core/c.md");
  });

  it("never splits a row from the qualifiers indented under it", () => {
    const payload = [
      "- [decisions/x] X [current] the decision",
      "    qualifiers: epistemic:speculation",
      "- [decisions/y] Y [current] the other decision",
    ].join("\n");
    const trimmed = trimToUnitBoundary(payload, 60);
    expect(trimmed === "" || trimmed.includes("qualifiers: epistemic:speculation")).toBe(true);
    expect(trimmed).not.toContain("decisions/y");
  });

  it("returns nothing when there is not even room for the stub", () => {
    expect(trimToUnitBoundary("- [a/b.md] ⚠ contradicts: a/c.md", 10)).toBe("");
  });

  it("leaves a payload that already fits byte-identical", () => {
    const payload = "- [a/b.md] one\n- [a/c.md] two";
    expect(trimToUnitBoundary(payload, 5000)).toBe(payload);
  });

  it("keeps the startup digest and the per-turn payload on separate budgets", async () => {
    const rows = Array.from({ length: 10 }, (_, i) => ({
      file: `core/m${i}.md`,
      name: `Memory ${i}`,
      summary: "x".repeat(80),
    }));
    const digest = await buildCoreDigest(
      { ...RCFG, coreMaxChars: 400, maxChars: 3000 },
      stubFetch({ "/context/prime": {}, "/list": rows }),
    );
    expect(digest!.length).toBeLessThanOrEqual(400);
    // Cut between rows: no half-row, and the last line is a whole one.
    for (const line of digest!.split("\n")) {
      if (line.startsWith("- [")) expect(line).toMatch(/^- \[core\/m\d+\.md\] Memory \d+ — x+$/);
    }
    // The per-turn channel spends its own cap, untouched by the startup one.
    const turn = await injected("current", 1100);
    expect(turn!.length).toBeLessThanOrEqual(1100);
    expect(turn).not.toContain("session start");
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
    const okFetch = stubFetch({ "/controls/check": { allowed: true }, "/session-end": { status: "ok" } });
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
      captureOn: false,
      resolveOn: true,
      resolveDeadlineMs: 250,
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

describe("lifecycle dry runs and retained copies", () => {
  const retained = {
    records: [
      {
        file: "insights/readout.md",
        of: "decisions/x.md",
        relations: ["sources (quoted)"],
        in_default_recall: true,
        action: "it quotes this record verbatim",
      },
    ],
    total: 3,
    more: 1,
    not_visible: 1,
    note: "Reported, never changed.",
  };

  it("archiveMemory posts the canonical params and returns the retained copies", async () => {
    const calls: Array<{ url: string; body?: unknown }> = [];
    const out = await archiveMemory(
      "decisions/x.md",
      CFG,
      stubFetch({ "/archive": { file: "decisions/x.md", status: "archived", retained_copies: retained } }, calls),
      "closed",
      "decisions/y.md",
    );
    expect(calls[0].url).toBe("http://test:6340/archive");
    expect(calls[0].body).toEqual({ file_path: "decisions/x.md", reason: "closed", superseded_by: "decisions/y.md" });
    expect(out?.retained_copies?.records[0].file).toBe("insights/readout.md");
    expect(out?.retained_copies?.more).toBe(1);
    expect(out?.retained_copies?.not_visible).toBe(1);
  });

  it("each of the four sends dry_run only when asked", async () => {
    const calls: Array<{ url: string; body?: unknown }> = [];
    const fetchFn = stubFetch(
      {
        "/archive": { file: "d.md", status: "would_archive", dry_run: true },
        "/restore": { file: "d.md", status: "would_restore", dry_run: true },
        "/unretract": { file: "d.md", status: "would_unretract", dry_run: true, mentions: 1 },
        "/forget-withdraw": { file: "f.md", status: "would_withdraw", dry_run: true, pref: "p" },
      },
      calls,
    );
    expect((await archiveMemory("d.md", CFG, fetchFn, undefined, undefined, true))?.status).toBe("would_archive");
    expect((await restoreMemory("d.md", CFG, fetchFn, undefined, true))?.status).toBe("would_restore");
    expect((await unretractMentions("d.md", "p", CFG, fetchFn, undefined, true))?.status).toBe("would_unretract");
    expect((await withdrawForgetRequest("f.md", CFG, fetchFn, undefined, true))?.status).toBe("would_withdraw");
    expect(calls.map((c) => (c.body as Record<string, unknown>).dry_run)).toEqual([true, true, true, true]);

    const plain: Array<{ url: string; body?: unknown }> = [];
    await archiveMemory("d.md", CFG, stubFetch({ "/archive": { file: "d.md", status: "archived" } }, plain));
    expect(plain[0].body).toEqual({ file_path: "d.md" });
  });
});


describe("capture opt-in and policy", () => {
  it("defaults off and rejects denied capture without sending content", async () => {
    expect(configFromEnv({}).captureOn).toBe(false);
    expect(configFromEnv({ PALINODE_CAPTURE_ENABLED: "1" }).captureOn).toBe(true);
    const calls: Array<{ url: string; body?: unknown }> = [];
    const payload = { summary: "FAKE_SECRET_excluded", project: "fixture", source: "pi-extension",
      harness: "pi", trigger: "session_shutdown", decisions: [], blockers: [], cwd: process.cwd() };
    expect(await postSessionCapture(payload, CFG,
      stubFetch({ "/controls/check": { allowed: false } }, calls))).toBe(false);
    expect(calls).toHaveLength(1);
    expect(JSON.stringify(calls)).not.toContain("FAKE_SECRET_excluded");
  });
});

describe("clientCwd — a linked worktree sends its main repository's root", () => {
  it("maps a linked worktree to the main worktree root, and leaves other dirs alone", () => {
    const base = realpathSync(mkdtempSync(join(tmpdir(), "pal-wt-")));
    const main = join(base, "harbor-notes");
    const linked = join(base, "agent-a1b2c3");
    const git = (...args: string[]) =>
      execFileSync("git", args, { stdio: "ignore", env: { ...process.env, GIT_CONFIG_NOSYSTEM: "1" } });
    git("init", "-q", main);
    git("-C", main, "-c", "user.email=t@t.test", "-c", "user.name=t",
      "commit", "-q", "--allow-empty", "-m", "init");
    git("-C", main, "worktree", "add", "-q", linked);

    expect(clientCwd(linked)).toBe(main);
    expect(clientCwd(main)).toBe(main);
    const plain = realpathSync(mkdtempSync(join(tmpdir(), "pal-plain-")));
    expect(clientCwd(plain)).toBe(plain);
  });
});
