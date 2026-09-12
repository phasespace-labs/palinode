/**
 * `openclaw.plugin.json` must be the schema `index.ts` actually accepts.
 *
 * The OpenClaw host validates plugin config against the manifest's
 * `configSchema` (Ajv, `additionalProperties: false`) before loading the
 * module, so a key `parse()` reads but the manifest omits is rejected at load
 * time — which is how `recallProfile`, an option the install guide documents,
 * came to be refused by a strict host while every test stayed green.
 *
 * Three invariants, checked from three directions:
 *   1. the committed manifest is byte-for-byte what `buildPluginManifest` renders
 *   2. every `cfg.<key>` read in `index.ts` is a schema property
 *   3. the install guide's config table and the schema list the same keys
 */

import * as fs from "fs";
import * as path from "path";
import { fileURLToPath } from "url";
import { describe, expect, it } from "vitest";
import { PROFILES } from "../index";
import { MANIFEST_FILENAME, buildPluginManifest, renderPluginManifest } from "../manifest";

const PLUGIN_ROOT = fileURLToPath(new URL("..", import.meta.url));
const read = (rel: string) => fs.readFileSync(path.join(PLUGIN_ROOT, rel), "utf-8");

const manifest = buildPluginManifest(PLUGIN_ROOT);
const schema = manifest.configSchema as {
  additionalProperties?: boolean;
  properties: Record<string, { enum?: string[] }>;
};
const schemaKeys = new Set(Object.keys(schema.properties));

describe("openclaw.plugin.json", () => {
  it("is what the runtime schema renders (regenerate with `npm run manifest`)", () => {
    expect(read(MANIFEST_FILENAME)).toBe(renderPluginManifest(manifest));
  });

  it("tracks package.json's version", () => {
    const pkg = JSON.parse(read("package.json")) as { version: string };
    expect(manifest.version).toBe(pkg.version);
  });

  it("rejects unknown keys and names the documented recall options", () => {
    expect(schema.additionalProperties).toBe(false);
    expect(schemaKeys).toContain("recallProfile");
    expect(schemaKeys).toContain("recallProfileConfig");
    expect(schemaKeys).toContain("midTurnMode");
    expect(schema.properties.recallProfile.enum).toEqual(Object.keys(PROFILES));
  });

  it("covers every config key index.ts reads", () => {
    const source = read("index.ts");
    const readKeys = new Set(
      Array.from(source.matchAll(/\bcfg\.([A-Za-z_]\w*)/g), (m) => m[1]),
    );
    expect(readKeys.size).toBeGreaterThan(0);
    for (const key of readKeys) {
      expect(schemaKeys, `index.ts reads cfg.${key}; add it to PALINODE_CONFIG_SCHEMA`).toContain(key);
    }
  });

  it("matches the config table in INSTALL.md in both directions", () => {
    const guide = read("INSTALL.md");
    const section = guide.slice(guide.indexOf("## Plugin config fields"));
    const documented = new Set(
      Array.from(section.matchAll(/^\| `(\w+)` \|/gm), (m) => m[1]),
    );
    expect(documented.size).toBeGreaterThan(0);
    for (const key of documented) {
      expect(schemaKeys, `INSTALL.md documents ${key} but the schema omits it`).toContain(key);
    }
    for (const key of schemaKeys) {
      expect(documented, `schema accepts ${key} but INSTALL.md does not document it`).toContain(key);
    }
  });

  it("lists exactly the tools register() registers", () => {
    expect(manifest.contracts.tools).toEqual([
      "palinode_search",
      "palinode_save",
      "palinode_ingest",
      "palinode_status",
      "palinode_diff",
      "palinode_blame",
      "palinode_depends",
    ]);
  });
});
