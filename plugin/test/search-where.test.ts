import * as path from "path";
import { afterEach, describe, expect, it } from "vitest";

import palinodePlugin, { describeFilterExclusion } from "../index";

type Execute = (id: string, params: Record<string, unknown>) => Promise<any>;

const originalFetch = global.fetch;

afterEach(() => {
  global.fetch = originalFetch;
});

function captureSearchTool(): { parameters: any; execute: Execute } {
  let captured: { parameters: any; execute: Execute } | undefined;
  const api: any = {
    pluginConfig: {
      palinodeDir: path.join(process.cwd(), "test-memory"),
      promptsDir: "specs/prompts",
      autoRecall: false,
      autoCapture: false,
      midTurnMode: "none",
    },
    logger: { info: () => undefined, warn: () => undefined, error: () => undefined },
    registerTool: (tool: any) => {
      if (typeof tool === "function") tool = tool({});
      if (tool.name === "palinode_search") captured = tool;
    },
    on: () => undefined,
    registerCli: () => undefined,
    registerService: () => undefined,
  };
  palinodePlugin.register(api);
  if (!captured) throw new Error("palinode_search was not registered");
  return captured;
}

describe("palinode_search takes where", () => {
  it("declares where as an array of strings", () => {
    const { parameters } = captureSearchTool();

    const where = parameters.properties.where;
    expect(where).toBeDefined();
    expect(where.type).toBe("array");
    expect(where.items.type).toBe("string");
    expect(parameters.required ?? []).not.toContain("where");
  });

  it("sends each entry as a list in the request body", async () => {
    const bodies: Record<string, unknown>[] = [];
    global.fetch = async (_url: any, init?: any) => {
      if (init?.body) bodies.push(JSON.parse(init.body));
      return new Response(JSON.stringify([]), { status: 200 });
    };
    const { execute } = captureSearchTool();

    await execute("call-1", { query: "release checklist", where: ["status=open", "entities=project/my-app"] });

    const searchBodies = bodies.filter((body) => body.query === "release checklist");
    expect(searchBodies).toHaveLength(1);
    expect(searchBodies[0].where).toEqual(["status=open", "entities=project/my-app"]);
  });

  it("sends no where field when the caller passes none", async () => {
    const bodies: Record<string, unknown>[] = [];
    global.fetch = async (_url: any, init?: any) => {
      if (init?.body) bodies.push(JSON.parse(init.body));
      return new Response(JSON.stringify([]), { status: 200 });
    };
    const { execute } = captureSearchTool();

    await execute("call-1", { query: "release checklist" });

    const searchBodies = bodies.filter((body) => body.query === "release checklist");
    expect(searchBodies).toHaveLength(1);
    expect(searchBodies[0]).not.toHaveProperty("where");
  });
});

function envelope(retrieval: Record<string, unknown>): string {
  return JSON.stringify({
    results: [],
    receipt: { bundle_id: "b", evaluated_at: "t", retrieval },
    project: "project/alpha",
    project_resolved_by: "environment",
  });
}

const EMPTY_RETRIEVAL = {
  active_mode: "hybrid",
  index_state: "ready",
  outcome: "no_match",
  confidence: "none",
  other_projects_withheld: 0,
};

describe("palinode_search says when a filter emptied the result", () => {
  it("names the where filter and the key no record in scope has", async () => {
    global.fetch = async (url: any) => new Response(
      String(url).endsWith("/search")
        ? envelope({
          ...EMPTY_RETRIEVAL,
          filter_exclusion: {
            by: ["where"], where: ["project=alpha"], matches: null,
            matches_at_least: false, missing_keys: ["project"],
          },
        })
        : JSON.stringify([]),
      { status: 200 },
    );
    const { execute } = captureSearchTool();

    const reply = await execute("call-1", { query: "ledger migration", where: ["project=alpha"] });

    expect(reply.content[0].text).toContain(
      "No record in scope matches where: project=alpha; no record in scope has the key " +
        "`project`. The filter, not the query, emptied this result.\n" +
        "No relevant memories found in Palinode.",
    );
  });

  it("says nothing about filters when the server reports no exclusion", async () => {
    global.fetch = async (url: any) => new Response(
      String(url).endsWith("/search") ? envelope(EMPTY_RETRIEVAL) : JSON.stringify([]),
      { status: 200 },
    );
    const { execute } = captureSearchTool();

    const reply = await execute("call-1", { query: "sonar calibration" });

    expect(reply.content[0].text).not.toContain("excluded by");
    expect(reply.content[0].text).not.toContain("No record in scope");
  });

  it("words a counted exclusion as the Python renderer does", () => {
    expect(describeFilterExclusion({
      filter_exclusion: {
        by: ["where", "types"], where: ["status=resolved"], types: ["Insight"],
        matches: 1, matches_at_least: false, missing_keys: [],
      },
    })).toBe(
      "1 match for this search was excluded by where: status=resolved and types: " +
        "Insight. The filters, not the query, emptied this result.",
    );
    expect(describeFilterExclusion({
      filter_exclusion: {
        by: ["types"], types: ["Decision"], matches: 25, matches_at_least: true, missing_keys: [],
      },
    })).toBe(
      "At least 25 matches for this search were excluded by types: Decision. " +
        "The filter, not the query, emptied this result.",
    );
    expect(describeFilterExclusion({ outcome: "no_match" })).toBe("");
  });
});
