import * as path from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import palinodePlugin from "../index";

const workspace = path.join(process.cwd(), "workspace-home");
const own = { file: "decisions/home.md", entities: ["project/HOME"], core: true, summary: "HOME MEMORY" };
const global = { file: "decisions/global.md", entities: [], core: true, summary: "GLOBAL MEMORY" };
const foreign = { file: "decisions/other.md", entities: "project/other", core: true, summary: "FOREIGN MEMORY" };
const files = [foreign, own, global];
let requests: Array<{ endpoint: string; body: any; file?: string | null }>;
let resolved: string | null;
let listingFails: boolean;
let controlsFail: boolean;
let legacyRefs: boolean;

function hit(file: typeof own | typeof foreign | typeof global) {
  return { content: file.summary, snippet: file.summary, category: "decisions", score: 1,
    file_path: `/server/memory/${file.file}`, rel_path: file.file };
}

function register(source = "semantic") {
  const hooks: Record<string, any> = {};
  const factories: Record<string, any> = {};
  const commands: Record<string, any> = {};
  const program = {
    command(name: string) {
      const command = { description: () => command, argument: () => command, option: () => command,
        action: (fn: any) => { commands[name] = fn; return command; }, command: program.command };
      return command;
    },
  };
  palinodePlugin.register({
    pluginConfig: { palinodeApiUrl: "http://fixture.test", palinodeDir: path.join(process.cwd(), "client-memory"),
      autoRecall: true, autoCapture: false,
      recallProfileConfig: { sources: [source], triggersLimit: 1 } },
    logger: { info: vi.fn(), warn: vi.fn(), error: vi.fn() },
    on: (name: string, hook: any) => { hooks[name] = hook; },
    registerTool: (tool: any, meta: any) => { factories[meta?.name ?? tool.name] = tool; },
    registerCli: (fn: any) => fn({ program }), registerService: vi.fn(),
  } as any);
  return {
    turn: (context: any = { workspaceDir: workspace }, event: any = {}) =>
      hooks.before_prompt_build({ prompt: "Recall the deployment decision for our service", ...event }, context),
    tool: (dir = workspace) => typeof factories.palinode_search === "function"
      ? factories.palinode_search({ workspaceDir: dir }) : factories.palinode_search,
    search: commands.search,
  };
}

beforeEach(() => {
  requests = [];
  resolved = "home";
  listingFails = false;
  controlsFail = false;
  legacyRefs = false;
  vi.stubGlobal("fetch", vi.fn(async (input: string, options?: RequestInit) => {
    const url = new URL(input);
    const body = options?.body ? JSON.parse(String(options.body)) : {};
    requests.push({ endpoint: url.pathname, body, file: url.searchParams.get("file_path") });
    if (url.pathname === "/controls/check") {
      if (controlsFail) return new Response("unavailable", { status: 503 });
      return Response.json({ allowed: true, project: body.automatic ? resolved : null });
    }
    if (url.pathname === "/search") {
      // Match the real API: only context scopes search; cwd/project are ignored.
      const scoped = body.context?.includes("project/home") && !body.include_other_projects;
      const rows = (scoped ? [own, global] : files).map(hit);
      if (body.receipt) return Response.json({ results: rows, project: "project/home",
        receipt: { retrieval: { other_projects_withheld: scoped ? 1 : 0 } } });
      return Response.json(rows);
    }
    // Neither endpoint accepts context. Sending that field must not fix this fixture.
    if (url.pathname === "/search-associative") return Response.json(files.map(hit));
    if (url.pathname === "/check-triggers") return Response.json(files.map(file => ({
      memory_file: file.file, description: file.summary,
    })));
    if (url.pathname === "/list") return listingFails
      ? new Response("unavailable", { status: 503 }) : Response.json(files);
    if (url.pathname === "/read") return Response.json({
      content: (legacyRefs
        ? `---\ncross_refs: [${url.searchParams.get("project") === "home" ? "decisions/home-link" : "decisions/foreign-link"}]\n---\n`
        : "") + files.find(file => file.file === url.searchParams.get("file_path"))?.summary,
    });
    throw new Error(`Unexpected endpoint ${url.pathname}`);
  }));
});

afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe("project isolation on automatic recall", () => {
  it.each(["semantic", "associative", "triggers", "core"])("withholds another project's %s memory", async source => {
    const result = await register(source).turn();
    expect(result.systemContext).not.toContain("FOREIGN MEMORY");
    expect(result.systemContext).not.toContain("other.md");
    expect(result.systemContext).toContain("HOME MEMORY");
    if (source !== "triggers") expect(result.systemContext).toContain("GLOBAL MEMORY");
    const checks = requests.filter(r => r.endpoint === "/controls/check");
    // Initial permission/scope check and final pause check; no extra resolution trip.
    expect(checks).toHaveLength(2);
    expect(checks[0].body).toMatchObject({ automatic: true, cwd: workspace });
    if (source === "semantic") expect(requests.find(r => r.endpoint === "/search")?.body.context)
      .toEqual(["project/home"]);
    if (source === "triggers" || source === "core") expect(requests.filter(r => r.endpoint === "/read")
      .some(r => r.file === foreign.file)).toBe(false);
  });

  it("uses event cwd when the hook has no workspace", async () => {
    const result = await register().turn({}, { cwd: workspace });
    expect(result.systemContext).not.toContain("FOREIGN MEMORY");
    expect(requests[0].body.cwd).toBe(workspace);
  });

  it.each(["associative", "triggers"])("withholds %s when live selection is unavailable", async source => {
    listingFails = true;
    expect(await register(source).turn()).toBeUndefined();
    expect(requests.some(r => r.endpoint === "/read")).toBe(false);
  });

  it("retains unscoped semantic recall when no project resolves", async () => {
    resolved = null;
    expect((await register().turn()).systemContext).toContain("GLOBAL MEMORY");
    expect(requests.find(r => r.endpoint === "/search")?.body).not.toHaveProperty("context");
  });
});

describe("automatic cross-ref read scope", () => {
  it.each(["core", "triggers"])("filters legacy cross_refs on %s reads", async source => {
    legacyRefs = true;
    const result = await register(source).turn();
    expect(result.systemContext).toContain("decisions/home-link");
    expect(result.systemContext).not.toContain("decisions/foreign-link");
  });
});

describe("project isolation on explicit search", () => {
  it("withholds another project's agent-tool memory by default", async () => {
    const result = await register().tool().execute("scoped", { query: "deployment" });
    expect(result.content[0].text).not.toContain("FOREIGN MEMORY");
    expect(result.content[0].text).toContain("HOME MEMORY");
    expect(result.content[0].text).toContain("GLOBAL MEMORY");
    expect(requests[0].body).toMatchObject({ automatic: true, cwd: workspace });
    expect(requests[1].body.context).toEqual(["project/home"]);
  });

  it("preserves the cross-project opt-in and its scoped request", async () => {
    const result = await register().tool().execute("all", { query: "deployment", include_other_projects: true });
    expect(result.content[0].text).toContain("FOREIGN MEMORY");
    expect(requests.find(r => r.endpoint === "/search")?.body)
      .toMatchObject({ include_other_projects: true, context: ["project/home"] });
  });

  it("keeps each tool factory's workspace separate", async () => {
    const runtime = register();
    await runtime.tool().execute("first", { query: "deployment" });
    await runtime.tool(path.join(process.cwd(), "workspace-second")).execute("second", { query: "deployment" });
    expect(requests.filter(r => r.endpoint === "/controls/check").map(r => r.body.cwd))
      .toEqual([workspace, path.join(process.cwd(), "workspace-second")]);
  });

  it("withholds another project's CLI memory", async () => {
    const output = vi.spyOn(console, "log").mockImplementation(() => {});
    await register().search("deployment", { limit: "5" });
    expect(output).toHaveBeenCalled();
    const text = output.mock.calls[0][0];
    expect(text).not.toContain("FOREIGN MEMORY");
    expect(text).toContain("HOME MEMORY");
    expect(text).toContain("GLOBAL MEMORY");
    expect(requests[0].body).toMatchObject({ automatic: true, cwd: process.cwd() });
    expect(requests[1].body.context).toEqual(["project/home"]);
  });

  it.each(["tool", "CLI"])("does not fall back to unscoped %s search if resolution fails", async surface => {
    controlsFail = true;
    vi.spyOn(console, "error").mockImplementation(() => {});
    const runtime = register();
    if (surface === "tool") {
      expect((await runtime.tool().execute("failed", { query: "deployment" })).content[0].text)
        .toContain("search failed");
    } else await runtime.search("deployment", { limit: "5" });
    expect(requests.some(r => r.endpoint === "/search")).toBe(false);
  });
});
