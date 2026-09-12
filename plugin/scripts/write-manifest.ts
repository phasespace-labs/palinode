/**
 * Regenerate `openclaw.plugin.json` from the runtime schema.
 *
 *   npm run manifest            # rewrite the manifest
 *   npm run manifest -- --check # exit 1 if the committed manifest is stale
 *
 * Runs from the compiled tree (`dist/scripts/`), so it resolves the package
 * root from the working directory npm sets, not from its own location.
 */

import * as fs from "fs";
import * as path from "path";
import { MANIFEST_FILENAME, buildPluginManifest, renderPluginManifest } from "../manifest.js";

const root = process.cwd();
const target = path.join(root, MANIFEST_FILENAME);
const rendered = renderPluginManifest(buildPluginManifest(root));
const current = fs.existsSync(target) ? fs.readFileSync(target, "utf-8") : "";

if (process.argv.includes("--check")) {
  if (current === rendered) {
    console.log(`${MANIFEST_FILENAME} is current`);
  } else {
    console.error(`${MANIFEST_FILENAME} is stale — run \`npm run manifest\` and commit the result`);
    process.exit(1);
  }
} else if (current === rendered) {
  console.log(`${MANIFEST_FILENAME} unchanged`);
} else {
  fs.writeFileSync(target, rendered, "utf-8");
  console.log(`wrote ${MANIFEST_FILENAME}`);
}
