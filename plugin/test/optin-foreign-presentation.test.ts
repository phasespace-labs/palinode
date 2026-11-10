import * as path from "node:path";
import { beforeEach, describe, expect, it, vi } from "vitest";
import palinodePlugin from "../index";

// An agent scoped to ledger-4a1f20 opted in to other projects, read a decision
// tagged [other project: project/ledger-9c3e57] mid-line, and used its value.
// The two slugs share a base name. The tool now lists other projects' records
// after this project's own, under a heading, each opening with both projects.

const workspace = path.join(process.cwd(), "workspace-tessel");
const LABEL = "[other project: project/ledger-9c3e57 — not project/ledger-4a1f20]";
const HEADING =
  "Other projects — context only, not this project's decisions (this project: project/ledger-4a1f20)";

const foreign = {
  content: "Data team assigned batch ID atlas-5b2d0c19.", category: "decisions", score: 1,
  file_path: "/server/memory/decisions/data-team-assigned-batch-id.md",
  other_project: ["project/ledger-9c3e57"],
};
const own = {
  content: "Ledger exports run nightly at 02:00.", category: "insights", score: 0.9,
  file_path: "/server/memory/insights/ledger-exports.md",
};

function searchTool() {
  const factories: Record<string, any> = {};
  palinodePlugin.register({
    pluginConfig: { palinodeApiUrl: "http://fixture.test", palinodeDir: path.join(process.cwd(), "client-memory"),
      autoRecall: false, autoCapture: false },
    logger: { info: vi.fn(), warn: vi.fn(), error: vi.fn() },
    on: vi.fn(),
    registerTool: (tool: any, meta: any) => { factories[meta?.name ?? tool.name] = tool; },
    registerCli: vi.fn(), registerService: vi.fn(),
  } as any);
  const tool = factories.palinode_search;
  return typeof tool === "function" ? tool({ workspaceDir: workspace }) : tool;
}

let rows: any[];

beforeEach(() => {
  // The API's delivered order puts the foreign record first; the tool regroups it.
  rows = [foreign, own];
  vi.stubGlobal("fetch", vi.fn(async (input: string) => {
    const url = new URL(input);
    if (url.pathname === "/controls/check") return Response.json({ allowed: true, project: "ledger-4a1f20" });
    if (url.pathname === "/search") {
      return Response.json({ results: rows, project: "project/ledger-4a1f20",
        project_resolved_by: "environment", receipt: { retrieval: { other_projects_withheld: 0 } } });
    }
    return new Response("not found", { status: 404 });
  }));
});

describe("palinode_search opted-in records from other projects", () => {
  it("lists them after this project's own, under a heading, each naming both projects", async () => {
    const result = await searchTool().execute("t", { query: "batch id", include_other_projects: true });
    const text: string = result.content[0].text;
    expect(text).toContain(`2. ${LABEL} [decisions] Data team assigned batch ID atlas-5b2d0c19.`);
    expect(text).toContain(`1. [insights] Ledger exports run nightly at 02:00.`);
    expect(text.indexOf("Ledger exports")).toBeLessThan(text.indexOf(HEADING));
    expect(text.indexOf(HEADING)).toBeLessThan(text.indexOf(LABEL));
    expect(text).not.toContain("[other project: project/ledger-9c3e57]");
  });

  it("adds no heading when nothing came from another project", async () => {
    rows = [own];
    const result = await searchTool().execute("t", { query: "batch id" });
    expect(result.content[0].text).not.toContain("Other projects —");
  });

  it("says in the option's description that they are context only", () => {
    const tool = searchTool();
    const description: string = tool.parameters.properties.include_other_projects.description;
    expect(description).toContain("context only, never this project's decisions");
    expect(description).toContain("ask the user instead");
  });
});
