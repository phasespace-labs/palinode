/**
 * `openclaw.plugin.json` derivation.
 *
 * The OpenClaw host reads the manifest before it loads the plugin module and
 * validates `plugins.entries.<id>.config` against `configSchema` with Ajv —
 * the module's own `configSchema` export never takes part in that check. A
 * hand-maintained JSON copy therefore drifts from the TypeBox schema in
 * `index.ts` without any test noticing, and a documented option ends up
 * rejected at load time ("invalid config").
 *
 * This module builds the manifest from the runtime objects instead:
 *   - id / name / description from the plugin definition
 *   - version from package.json
 *   - configSchema from `PALINODE_CONFIG_SCHEMA`
 *   - contracts.tools from what `register()` actually registers
 *
 * `scripts/write-manifest.ts` writes it; `test/manifest.test.ts` fails when
 * the committed file no longer matches.
 */

import * as fs from "fs";
import * as path from "path";
import palinodePlugin, { PALINODE_CONFIG_SCHEMA } from "./index.js";

export const MANIFEST_FILENAME = "openclaw.plugin.json";

export type PluginManifest = {
  id: string;
  name: string;
  version: string;
  description: string;
  configSchema: Record<string, unknown>;
  contracts: { tools: string[] };
};

/** Run `register()` against a recording stub and collect the tool names. */
function registeredToolNames(): string[] {
  const names: string[] = [];
  const noop = () => {};
  const api = {
    pluginConfig: {},
    logger: { info: noop, warn: noop, error: noop },
    registerTool: (def: { name: string }) => {
      names.push(def.name);
    },
    on: noop,
    registerCli: noop,
    registerService: noop,
  };
  palinodePlugin.register(api);
  return names;
}

export function buildPluginManifest(pluginRoot: string): PluginManifest {
  const pkg = JSON.parse(fs.readFileSync(path.join(pluginRoot, "package.json"), "utf-8")) as {
    name: string;
    version: string;
  };
  if (pkg.name !== palinodePlugin.id) {
    throw new Error(`${pluginRoot}/package.json is "${pkg.name}", expected "${palinodePlugin.id}"`);
  }
  return {
    id: palinodePlugin.id,
    name: palinodePlugin.name,
    version: pkg.version,
    description: palinodePlugin.description,
    // The JSON round-trip drops TypeBox's symbol-keyed metadata; what remains is plain JSON Schema.
    configSchema: JSON.parse(JSON.stringify(PALINODE_CONFIG_SCHEMA)) as Record<string, unknown>,
    contracts: { tools: registeredToolNames() },
  };
}

export function renderPluginManifest(manifest: PluginManifest): string {
  return JSON.stringify(manifest, null, 2) + "\n";
}
