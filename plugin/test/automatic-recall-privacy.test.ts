import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import palinodePlugin from "../index";

let scratch: string;
let localDir: string;
let calls: Array<{ url: URL; options?: RequestInit }>;
const listed = [{ file: "decisions/visible & core.md", core: true, summary: "Server summary" }];
const body = "---\ncore: true\n---\nVISIBLE SERVER BODY";

function register(config: Record<string, unknown> = {}) {
  const hooks: Record<string, (event?: any) => Promise<any>> = {};
  const tools: Record<string, any> = {};
  const logger = { info: vi.fn(), warn: vi.fn(), error: vi.fn() };
  palinodePlugin.register({
    pluginConfig: { palinodeApiUrl: "http://fixture.test", palinodeDir: localDir,
      autoCapture: false, recallProfile: "writing", ...config },
    logger,
    registerTool: (tool: any) => {
      if (typeof tool === "function") tool = tool({});
      tools[tool.name] = tool;
    },
    on: (name: string, hook: any) => { hooks[name] = hook; },
    registerCli: vi.fn(), registerService: vi.fn(),
  } as any);
  return { hooks, tools, logger, turn: () => hooks.before_prompt_build({ prompt: "Please recall our deployment decision" }, { workspaceDir: localDir }) };
}

function api(respond: (url: URL, options?: RequestInit) => unknown = (url) =>
  url.pathname === "/list" ? listed : { content: body }) {
  vi.stubGlobal("fetch", vi.fn(async (input: string, options?: RequestInit) => {
    const url = new URL(input);
    if (url.pathname === "/controls/check") return Response.json({ allowed: true });
    calls.push({ url, options });
    const result = respond(url, options);
    return result instanceof Response ? result : Response.json(result);
  }));
}

beforeEach(() => {
  scratch = fs.mkdtempSync(path.join(os.tmpdir(), "palinode-plugin-privacy-"));
  localDir = path.join(scratch, "store");
  fs.mkdirSync(localDir);
  fs.mkdirSync(path.join(localDir, "decisions"));
  for (const visibility of ["private", "restricted"]) {
    fs.writeFileSync(path.join(localDir, "decisions", `${visibility}.md`),
      `---\ncore: true\nvisibility: ${visibility}\nsummary: HIDDEN ${visibility}\n---\nHIDDEN LOCAL ${visibility}`);
  }
  calls = [];
  vi.stubEnv("PALINODE_API_TOKEN", "");
  api();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
  fs.rmSync(scratch, { recursive: true, force: true });
});

describe("automatic core selection", () => {
  it("uses server selection and encoded reads, never local hidden cores", async () => {
    const result = await register().turn();
    expect(calls.map(({ url }) => url.pathname)).toEqual(["/list", "/read"]);
    expect(calls[0].url.search).toBe("?core_only=true");
    expect(calls[1].url.searchParams.get("file_path")).toBe(listed[0].file);
    expect(result.systemContext).toContain(body);
    expect(result.systemContext).toContain(listed[0].file);
    expect(result.systemContext).toContain("Server summary");
    expect(result.systemContext).not.toContain("HIDDEN");
    expect(calls[0].options?.signal).toBe(calls[1].options?.signal);
  });

  it.each([401, 403, 500])("reports HTTP %s without a local fallback", async (status) => {
    api(() => new Response("unavailable", { status }));
    const runtime = register();
    expect(await runtime.turn()).toBeUndefined();
    expect(calls).toHaveLength(1);
    expect(runtime.logger.warn).toHaveBeenCalledWith(expect.stringContaining("core recall unavailable"));
  });

  it.each(["network", "malformed", "read failure"])("fails closed on %s", async (failure) => {
    api((url) => {
      if (failure === "network") throw new Error("offline");
      if (failure === "malformed") return { files: listed };
      return url.pathname === "/list" ? listed : new Response("gone", { status: 404 });
    });
    const runtime = register();
    expect(await runtime.turn()).toBeUndefined();
    expect(runtime.logger.warn).toHaveBeenCalled();
  });

  it("treats an empty visible selection as empty recall", async () => {
    api(() => []);
    const runtime = register();
    expect(await runtime.turn()).toBeUndefined();
    expect(runtime.logger.warn).not.toHaveBeenCalled();
  });

  it("reselects after compaction, and skips ordinary mid-turn core by default", async () => {
    const runtime = register();
    expect((await runtime.turn()).systemContext).toContain(body);
    expect(await runtime.turn()).toBeUndefined();
    expect(calls).toHaveLength(2);
    await runtime.hooks.after_compaction();
    expect((await runtime.turn()).systemContext).toContain(body);
    expect(calls).toHaveLength(4);
  });

  it("reselects and uses parsed summaries without reads on summary turns", async () => {
    const runtime = register({ midTurnMode: "summary" });
    await runtime.turn();
    calls = [];
    const result = await runtime.turn();
    expect(calls.map(({ url }) => url.pathname)).toEqual(["/list"]);
    expect(result.systemContext).toContain("> Server summary");
    expect(result.systemContext).not.toContain("VISIBLE SERVER BODY");
    api(() => []);
    expect(await runtime.turn()).toBeUndefined();
  });

  it("skips absent summaries and honors full mid-turn mode", async () => {
    api((url) => url.pathname === "/list" ? [{ ...listed[0], summary: "" }] : { content: body });
    const summary = register({ midTurnMode: "summary" });
    await summary.turn();
    expect(await summary.turn()).toBeUndefined();
    const full = register({ midTurnMode: "full" });
    await full.turn();
    expect((await full.turn()).systemContext).toContain(body);
  });

  it("preserves the periodic full-core fallback", async () => {
    const runtime = register();
    await runtime.turn();
    for (let turn = 2; turn < 200; turn++) expect(await runtime.turn()).toBeUndefined();
    expect((await runtime.turn()).systemContext).toContain(body);
    expect(calls).toHaveLength(4);
  });

  it.each(["off", "minimal", "monitoring", "investigation"])("%s does not discover core", async (recallProfile) => {
    api(() => []);
    await register({ recallProfile }).turn();
    expect(calls.some(({ url }) => url.pathname === "/list")).toBe(false);
  });

  it("disables hooks with autoRecall false", () => {
    expect(register({ autoRecall: false }).hooks.before_prompt_build).toBeUndefined();
    expect(calls).toHaveLength(0);
  });

  it("applies per-file and aggregate core caps and stops reading at the budget", async () => {
    api((url) => url.pathname === "/list" ? [...listed, { ...listed[0], file: "decisions/next.md" }] : { content: "X".repeat(500) });
    const result = await register({ recallProfileConfig: { coreMaxCharsPerFile: 20, coreBudget: 100 } }).turn();
    expect(result.systemContext).toContain("X".repeat(20));
    expect(result.systemContext).not.toContain("X".repeat(21));
    expect(result.systemContext).toContain("100 / 100 chars");
    expect(calls.filter(({ url }) => url.pathname === "/read")).toHaveLength(1);
  });

  it("caps summary core and preserves total-profile truncation", async () => {
    api((url) => url.pathname === "/list" ? [{ ...listed[0], summary: "S".repeat(500) }] : { content: body });
    const runtime = register({ midTurnMode: "summary", recallProfileConfig: { coreBudget: 100, totalBudget: 180 } });
    await runtime.turn();
    const result = await runtime.turn();
    expect(result.systemContext).toContain("100 / 100 chars");
    expect(result.systemContext).toContain("truncated by recallProfile total budget: 180 chars");
  });

  it("keeps independent search and associative recall when core is unavailable", async () => {
    api((url) => {
      if (url.pathname === "/list") return new Response("unavailable", { status: 503 });
      if (url.pathname === "/search") return [{ content: "VISIBLE SEARCH", category: "decisions" }];
      if (url.pathname === "/search-associative") return [{ content: "VISIBLE ASSOCIATIVE", file_path: "decisions/related.md" }];
      return [];
    });
    const result = await register({ recallProfile: "coding" }).turn();
    expect(result.systemContext).toContain("VISIBLE SEARCH");
    expect(result.systemContext).toContain("VISIBLE ASSOCIATIVE");
    expect(result.systemContext).not.toContain("HIDDEN");
  });
});

describe("trigger selection and deployment authentication", () => {
  const triggers = [
    { memory_file: "decisions/private.md", description: "HIDDEN PRIVATE TRIGGER" },
    { memory_file: "decisions/restricted.md", description: "HIDDEN RESTRICTED TRIGGER" },
    { memory_file: listed[0].file, description: "Visible trigger" },
  ];

  it("filters hidden trigger metadata before applying trigger limits and reads via API", async () => {
    api((url) => url.pathname === "/check-triggers" ? triggers : url.pathname === "/list" ? listed : { content: body });
    const result = await register({ recallProfile: "monitoring", recallProfileConfig: { triggersLimit: 1, triggersMaxCharsEach: 10 } }).turn();
    expect(calls.map(({ url }) => url.pathname)).toEqual(["/check-triggers", "/list", "/read"]);
    expect(calls[1].url.search).toBe("");
    expect(calls[2].url.searchParams.get("file_path")).toBe(listed[0].file);
    expect(result.systemContext).toContain("Visible trigger");
    expect(result.systemContext).toContain(body.slice(0, 10));
    expect(result.systemContext).not.toContain("VISIBLE SERVER BODY");
    expect(result.systemContext).not.toContain("HIDDEN");
    expect(result.systemContext).not.toContain("private.md");
    expect(result.systemContext).not.toContain("restricted.md");
  });

  it.each(["selection", "read"])("does not use local trigger files after %s failure", async (failure) => {
    api((url) => {
      if (url.pathname === "/check-triggers") return triggers;
      if (url.pathname === "/list" && failure === "read") return listed;
      return new Response("unavailable", { status: 503 });
    });
    const runtime = register({ recallProfile: "monitoring" });
    expect(await runtime.turn()).toBeUndefined();
    expect(runtime.logger.warn).toHaveBeenCalledWith(expect.stringContaining("trigger recall unavailable"));
  });

  it.each(["", " fixture-deployment-token "])("forwards only a configured env bearer on tools and recall (%s)", async (token) => {
    vi.stubEnv("PALINODE_API_TOKEN", token);
    api((url) => url.pathname === "/check-triggers" ? triggers : url.pathname === "/list" ? listed : { content: body });
    const runtime = register({ recallProfile: "conversation" });
    await runtime.turn();
    await runtime.tools.palinode_blame.execute("explicit", { file: "decisions/private.md" });
    expect(calls.at(-1)?.url.pathname).toBe("/blame/decisions/private.md");
    for (const call of calls) {
      const headers = new Headers(call.options?.headers);
      expect(headers.get("Authorization")).toBe(token.trim() ? `Bearer ${token.trim()}` : null);
      expect(headers.get("Content-Type")).toBe("application/json");
    }
    expect(JSON.stringify(runtime.logger.info.mock.calls)).not.toContain("fixture-deployment-token");
    expect(JSON.stringify(runtime.logger.warn.mock.calls)).not.toContain("fixture-deployment-token");
  });
});
