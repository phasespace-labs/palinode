/**
 * ADR-022: the plugin is checked against the operation spec, generated per param.
 *
 * The spec lives in `palinode/core/operations.py`. `pretest` dumps it into
 * `parity-registry.json` (`specs`). For each spec param this file checks that
 * the plugin tool advertises it with the spec's type and enum, forwards a set
 * value under its canonical name, and sends nothing the caller did not set, so
 * the server's defaults apply. Its Python counterpart for MCP and the CLI is
 * `tests/test_operation_spec.py`.
 */
import { afterEach, beforeAll, describe, expect, it } from "vitest";
import * as fs from "fs";
import * as path from "path";
import { fileURLToPath } from "url";

import palinodePlugin from "../index";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

interface SpecParam {
  name: string;
  type: string;
  required: boolean;
  enum: string[] | null;
  server_default: string | null;
  flag: boolean;
  omit_if_empty: boolean;
  unset_values: unknown[];
  /** The named fields of an object, or of each item of an array of objects. */
  fields: SpecParam[];
}

interface OperationSpec {
  name: string;
  endpoint: string;
  params: SpecParam[];
}

/**
 * The plugin tool each migrated operation is exposed as. An operation the
 * plugin does not expose (ADR-019: a plugin joins only for its own tools) is
 * absent and skipped.
 */
const PLUGIN_TOOL: Record<string, string> = {
  search: "palinode_search",
  save: "palinode_save",
};

function loadSpecs(): OperationSpec[] {
  const file = path.resolve(__dirname, "..", "parity-registry.json");
  return (JSON.parse(fs.readFileSync(file, "utf-8")).specs ?? []) as OperationSpec[];
}

interface Tool {
  parameters: { properties: Record<string, any>; required?: string[] };
  execute: (id: string, params: any) => Promise<any>;
}

function captureTool(name: string): Tool {
  let found: Tool | undefined;
  palinodePlugin.register({
    pluginConfig: { autoRecall: false, autoCapture: false, midTurnMode: "none" },
    logger: { info() {}, warn() {}, error() {} },
    registerTool(def: any) {
      if (typeof def === "function") def = def({});
      if (def?.name === name) found = def;
    },
    on() {}, registerCli() {}, registerService() {},
  } as any);
  if (!found) throw new Error(`plugin registers no ${name}`);
  return found;
}

/** An object carrying every field, nested ones included. */
function object(fields: SpecParam[]): Record<string, unknown> {
  return Object.fromEntries(fields.map((f) => [f.name, sample(f)[1]]));
}

/** A valid, non-default value for the param, and what the body must carry. */
function sample(p: SpecParam): [unknown, unknown] {
  if (p.type === "object") {
    const v = p.fields.length ? object(p.fields) : { k: "v" };
    return [v, v];
  }
  if (p.type === "array" && p.fields.length) {
    const v = [object(p.fields)];
    return [v, v];
  }
  if (p.type === "boolean") return [true, true];
  if (p.type === "integer") return [2, 2];
  if (p.type === "number") return [0.33, 0.33];
  if (p.type === "array") return p.enum ? [[p.enum[0]], [p.enum[0]]] : [["status=open"], ["status=open"]];
  if (p.enum) {
    const v = p.enum.filter((e) => !p.unset_values.includes(e)).at(-1)!;
    return [v, v];
  }
  return ["2026-01-01", "2026-01-01"];
}

/** The spec's required args for a call: every required param, sampled. */
function requiredArgs(spec: OperationSpec): Record<string, unknown> {
  return Object.fromEntries(
    spec.params.filter((p) => p.required).map((p) => [p.name, p.enum ? p.enum[0] : sample(p)[0]]),
  );
}

/** The nested fields a TypeBox object schema declares match the spec's. */
function expectFields(schema: any, fields: SpecParam[], where: string): void {
  expect(Object.keys(schema.properties ?? {}).sort(), where).toEqual(fields.map((f) => f.name).sort());
  expect([...(schema.required ?? [])].sort(), `${where} required`).toEqual(
    fields.filter((f) => f.required).map((f) => f.name).sort(),
  );
  for (const f of fields) {
    const prop = schema.properties[f.name];
    expect(prop.type, `${where}.${f.name}`).toBe(f.type);
    if (f.fields.length) {
      expectFields(f.type === "array" ? prop.items : prop, f.fields, `${where}.${f.name}`);
    }
  }
}

/** The literal values a TypeBox property accepts, if it is an enum. */
function literals(prop: any): string[] | null {
  const union = prop.anyOf ?? prop.items?.anyOf;
  if (!union) return null;
  return union.map((member: any) => member.const).filter((v: unknown) => typeof v === "string");
}

const originalFetch = global.fetch;
let bodies: Record<string, any>[] = [];

function captureFetch(endpoint: string): void {
  bodies = [];
  global.fetch = (async (url: any, opts?: any) => {
    if (new URL(String(url)).pathname === endpoint) bodies.push(JSON.parse(opts?.body ?? "{}"));
    return { ok: true, json: async () => [] } as any;
  }) as any;
}

afterEach(() => {
  global.fetch = originalFetch;
});

const specs = loadSpecs();

describe("ADR-022 operation specs reach the plugin", () => {
  it("the registry carries the specs", () => {
    expect(specs.map((s) => s.name)).toContain("search");
  });

  for (const spec of specs.filter((s) => PLUGIN_TOOL[s.name])) {
    const toolName = PLUGIN_TOOL[spec.name];
    let tool: Tool;
    beforeAll(() => {
      tool = captureTool(toolName);
    });

    it(`${toolName} advertises every spec param with its type and enum`, () => {
      const props = tool.parameters.properties;
      expect(Object.keys(props).sort()).toEqual(spec.params.map((p) => p.name).sort());
      for (const p of spec.params) {
        const prop = props[p.name];
        if (p.enum && p.type !== "array") {
          expect(literals(prop)?.sort(), p.name).toEqual([...p.enum].sort());
        } else {
          expect(prop.type, p.name).toBe(p.type);
        }
        if (p.type === "array" && p.enum) {
          expect(literals(prop)?.sort(), p.name).toEqual([...p.enum].sort());
        }
        expect(prop.default, `${p.name} must not advertise a default`).toBeUndefined();
        if (p.fields.length) {
          expectFields(p.type === "array" ? prop.items : prop, p.fields, p.name);
        }
      }
      expect(tool.parameters.required ?? []).toEqual(
        spec.params.filter((p) => p.required).map((p) => p.name),
      );
    });

    for (const p of spec.params.filter((p) => !p.required)) {
      it(`${toolName} forwards ${p.name}`, async () => {
        captureFetch(spec.endpoint);
        const [given, expected] = sample(p);
        await tool.execute("call", { ...requiredArgs(spec), [p.name]: given });
        expect(bodies).toHaveLength(1);
        expect(bodies[0][p.name]).toEqual(expected);
      });
    }

    it(`${toolName} sends nothing the caller did not set`, async () => {
      captureFetch(spec.endpoint);
      await tool.execute("call", requiredArgs(spec));
      expect(bodies).toHaveLength(1);
      const sent = Object.keys(bodies[0]).filter((k) => spec.params.some((p) => p.name === k));
      expect(sent.sort()).toEqual(spec.params.filter((p) => p.required).map((p) => p.name).sort());
    });
  }
});
