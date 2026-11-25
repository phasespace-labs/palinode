/**
 * Auto-recall type filtering.
 *
 * A profile's `typeAllow` reaches `/search` as the server's `types` allow-list,
 * holding only real memory types: `types` is validated against the memory-type
 * enum, so one unknown value would fail the whole request. Values that are not
 * memory types are dropped with a single warning per process. `/search-associative`
 * takes the same `types` and `type_deny` filters, built the same way.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

type Body = Record<string, unknown>;

let bodies: Array<{ path: string; body: Body }>;

beforeEach(() => {
  // The dropped-value warning is once per process (module state), so every
  // test gets a fresh copy of the plugin module.
  vi.resetModules();
  bodies = [];
  vi.stubEnv("PALINODE_API_TOKEN", "");
  vi.stubGlobal("fetch", vi.fn(async (input: string, options?: RequestInit) => {
    const url = new URL(input);
    if (url.pathname === "/controls/check") return Response.json({ allowed: true });
    if (typeof options?.body === "string") {
      bodies.push({ path: url.pathname, body: JSON.parse(options.body) });
    }
    return Response.json([]);
  }));
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

async function register(config: Record<string, unknown> = {}) {
  const { default: palinodePlugin } = await import("../index");
  const hooks: Record<string, (event?: any, ctx?: any) => Promise<any>> = {};
  const logger = { info: vi.fn(), warn: vi.fn(), error: vi.fn() };
  palinodePlugin.register({
    pluginConfig: {
      palinodeApiUrl: "http://fixture.test",
      palinodeDir: "/nonexistent/palinode-store",
      autoCapture: false,
      autoRecall: true,
      recallProfile: "investigation",
      ...config,
    },
    logger,
    registerTool: () => undefined,
    on: (name: string, hook: any) => { hooks[name] = hook; },
    registerCli: vi.fn(),
    registerService: vi.fn(),
  } as any);
  return {
    logger,
    turn: () => hooks.before_prompt_build(
      { prompt: "Why did the ledger migration fail last week?" },
      // Automatic recall runs only for an absolute workspace directory.
      { workspaceDir: "/nonexistent/workspace" },
    ),
  };
}

function sent(path: string): Body[] {
  return bodies.filter((entry) => entry.path === path).map((entry) => entry.body);
}

function typeWarnings(logger: { warn: ReturnType<typeof vi.fn> }): string[] {
  return logger.warn.mock.calls
    .map((call) => String(call[0]))
    .filter((message) => message.includes("typeAllow"));
}

describe("auto-recall typeAllow", () => {
  it("sends the investigation profile's types to /search", async () => {
    const runtime = await register();
    await runtime.turn();

    const search = sent("/search");
    expect(search).toHaveLength(1);
    expect(search[0].types).toEqual(["Decision", "Insight"]);
    expect(search[0]).not.toHaveProperty("type_allow");
    expect(typeWarnings(runtime.logger)).toEqual([]);
  });

  it("sends only the valid values and warns once across two recalls", async () => {
    const runtime = await register({
      recallProfileConfig: { typeAllow: ["Decision", "RCA", "Insight", "Postmortem"] },
    });
    await runtime.turn();
    await runtime.turn();

    const search = sent("/search");
    expect(search).toHaveLength(2);
    for (const body of search) {
      expect(body.types).toEqual(["Decision", "Insight"]);
      expect(body).not.toHaveProperty("type_allow");
    }
    const warnings = typeWarnings(runtime.logger);
    expect(warnings).toHaveLength(1);
    expect(warnings[0]).toContain("RCA");
    expect(warnings[0]).toContain("Postmortem");
  });

  it("sends no types when no value is a memory type", async () => {
    const runtime = await register({
      recallProfileConfig: { typeAllow: ["RCA", "Incident"] },
    });
    await runtime.turn();

    const search = sent("/search");
    expect(search).toHaveLength(1);
    expect(search[0]).not.toHaveProperty("types");
    expect(search[0]).not.toHaveProperty("type_allow");
    expect(typeWarnings(runtime.logger)).toHaveLength(1);
  });

  it("sends the same type filters to /search-associative", async () => {
    const runtime = await register({
      recallProfileConfig: {
        typeAllow: ["Decision", "RCA", "Insight"],
        typeDeny: ["ProjectSnapshot"],
      },
    });
    await runtime.turn();

    const assoc = sent("/search-associative");
    expect(assoc).toHaveLength(1);
    expect(assoc[0].types).toEqual(["Decision", "Insight"]);
    expect(assoc[0].type_deny).toEqual(["ProjectSnapshot"]);
    expect(assoc[0]).not.toHaveProperty("type_allow");
    // One warning for the dropped value, though both calls dropped it.
    expect(typeWarnings(runtime.logger)).toHaveLength(1);
  });

  it("sends no types to /search-associative when no value is a memory type", async () => {
    const runtime = await register({
      recallProfileConfig: { typeAllow: ["RCA"] },
    });
    await runtime.turn();

    const assoc = sent("/search-associative");
    expect(assoc).toHaveLength(1);
    expect(assoc[0]).not.toHaveProperty("types");
  });
});
