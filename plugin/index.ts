/**
 * OpenClaw Palinode Plugin
 *
 * Persistent memory system — files as truth, vectors as search.
 * Connects to the Palinode Python API for search/save operations.
 * Injects core memory at session start, extracts memories at session end.
 *
 * Host-side install / opt-in flags: see plugin/INSTALL.md
 */

import * as fs from "fs";
import * as path from "path";
import { Type } from "@sinclair/typebox";
import type { OpenClawPluginApi } from "openclaw/plugin-sdk";

// ============================================================================
// Config
// ============================================================================

const DEFAULTS = {
  palinodeApiUrl: "http://localhost:6340",
  palinodeDir: path.join(process.env.HOME || "", "palinode"),
  promptsDir: "specs/prompts",
  autoCapture: true,
  autoRecall: true,
  midTurnMode: "none" as "none" | "summary" | "full",
  recallProfile: "coding" as RecallProfileName,
};

// ----------------------------------------------------------------------------
// Recall profiles (#391 / #394)
//
// Pre-composed points in the (sources × types × per-source caps × total budget)
// space. The plugin owns "which profile" via the OpenClaw config; the Palinode
// server exposes primitives (max_chars, type_allow/deny). Profile names are a
// plugin-level vocabulary — Palinode does not need to know what "monitoring"
// means.
//
// Modal taxonomy the presets sit on:
//   - acting     (probe/deploy/ingest) — operational state, deny past-tense reflection
//   - deciding   (architecture/design) — past decisions + rationale
//   - investigating (diagnosis) — RCAs, postmortems, incidents
//   - composing  (writing/code) — patterns, voice, style
//   - conversing (open chat) — people/preferences, recent state
// ----------------------------------------------------------------------------

export type RecallSource = "core" | "semantic" | "associative" | "triggers";

export type RecallProfileConfig = {
  sources: RecallSource[];
  coreMaxCharsPerFile?: number;
  coreBudget?: number;
  semanticLimit?: number;
  semanticMaxChars?: number;
  associativeLimit?: number;
  associativeMaxChars?: number;
  triggersLimit?: number;
  triggersMaxCharsEach?: number;
  typeAllow?: string[];     // forwarded to /search via type_allow once server supports it
  typeDeny?: string[];
  totalBudget?: number;     // hard cap across all sources; injection clipped here
};

export type RecallProfileName =
  | "coding"
  | "monitoring"
  | "investigation"
  | "writing"
  | "conversation"
  | "minimal"
  | "off";

export const PROFILES: Record<RecallProfileName, RecallProfileConfig> = {
  coding: {
    sources: ["core", "semantic", "associative", "triggers"],
    coreMaxCharsPerFile: 3000,
    coreBudget: 8000,
    semanticLimit: 5,
    semanticMaxChars: 700,
    associativeLimit: 3,
    associativeMaxChars: 500,
    triggersLimit: 10,
    triggersMaxCharsEach: 2000,
    totalBudget: 25000,
  },
  monitoring: {
    sources: ["triggers"],
    triggersLimit: 3,
    triggersMaxCharsEach: 1000,
    typeDeny: ["RCA", "Postmortem", "Incident", "Reflection"],
    totalBudget: 3000,
  },
  investigation: {
    sources: ["semantic", "associative"],
    semanticLimit: 8,
    semanticMaxChars: 1500,
    associativeLimit: 5,
    associativeMaxChars: 1000,
    typeAllow: ["RCA", "Postmortem", "Incident", "Decision", "Insight"],
    totalBudget: 25000,
  },
  writing: {
    sources: ["core"],
    coreMaxCharsPerFile: 2500,
    coreBudget: 6000,
    totalBudget: 6000,
  },
  conversation: {
    sources: ["core", "triggers"],
    coreMaxCharsPerFile: 2000,
    coreBudget: 3000,
    triggersLimit: 5,
    triggersMaxCharsEach: 800,
    totalBudget: 5000,
  },
  minimal: { sources: [], totalBudget: 0 },
  off:     { sources: [], totalBudget: 0 },
};

type PalinodeConfig = {
  palinodeApiUrl: string;
  palinodeDir: string;
  promptsDir: string;
  autoCapture: boolean;
  autoRecall: boolean;
  midTurnMode: "none" | "summary" | "full";
  recallProfile: RecallProfileName;
  recallProfileConfig?: Partial<RecallProfileConfig>;  // per-field override over the named preset
};

// Config schema follows standard OpenClaw plugin pattern
const _palinodeConfigSchema = Type.Object({
  palinodeApiUrl: Type.String({ default: DEFAULTS.palinodeApiUrl }),
  palinodeDir: Type.String({ default: DEFAULTS.palinodeDir }),
  promptsDir: Type.String({ default: DEFAULTS.promptsDir }),
  autoCapture: Type.Boolean({ default: DEFAULTS.autoCapture }),
  autoRecall: Type.Boolean({ default: DEFAULTS.autoRecall }),
  recallProfile: Type.String({ default: DEFAULTS.recallProfile }),
});

const palinodeConfigSchema = {
  ..._palinodeConfigSchema,
  parse(value: unknown): PalinodeConfig {
    const cfg = (value || {}) as Record<string, unknown>;
    const profileName = (typeof cfg.recallProfile === "string" && (cfg.recallProfile as string) in PROFILES)
      ? (cfg.recallProfile as RecallProfileName)
      : DEFAULTS.recallProfile;
    const overrideRaw = cfg.recallProfileConfig;
    const recallProfileConfig =
      (overrideRaw && typeof overrideRaw === "object" && !Array.isArray(overrideRaw))
        ? (overrideRaw as Partial<RecallProfileConfig>)
        : undefined;
    return {
      palinodeApiUrl:
        typeof cfg.palinodeApiUrl === "string"
          ? cfg.palinodeApiUrl
          : DEFAULTS.palinodeApiUrl,
      palinodeDir:
        typeof cfg.palinodeDir === "string"
          ? cfg.palinodeDir
          : DEFAULTS.palinodeDir,
      promptsDir:
        typeof cfg.promptsDir === "string"
          ? cfg.promptsDir
          : DEFAULTS.promptsDir,
      autoCapture: cfg.autoCapture !== false,
      autoRecall: cfg.autoRecall !== false,
      midTurnMode: (["none", "summary", "full"].includes(cfg.midTurnMode as string) ? cfg.midTurnMode : "none") as "none" | "summary" | "full",
      recallProfile: profileName,
      recallProfileConfig,
    };
  },
};

/**
 * Compose the active recall configuration from the named profile + override.
 *
 * - `autoRecall: false` short-circuits to "off" (empty sources, zero budget).
 * - Unknown profile names fall back to "coding" defensively at parse time.
 * - `recallProfileConfig` shallow-overrides individual fields on the base preset,
 *   so callers can ship `recallProfile: "monitoring"` and bump `triggersLimit`
 *   to 5 without forking the whole preset.
 */
export function resolveProfile(cfg: Pick<PalinodeConfig, "autoRecall" | "recallProfile" | "recallProfileConfig">): RecallProfileConfig {
  if (cfg.autoRecall === false) return PROFILES.off;
  const base = PROFILES[cfg.recallProfile] ?? PROFILES.coding;
  return { ...base, ...(cfg.recallProfileConfig ?? {}) };
}

export type InjectionParts = {
  coreContent?: string;
  topicContent?: string;
  assocContent?: string;
  triggerContent?: string;
  coreBudget?: number;
};

export type InjectionFrame =
  | { kind: "system"; systemContext: string; prependContext?: never }
  | { kind: "fallback"; prependContext: string; systemContext?: never };

export function composeInjection(parts: InjectionParts, profileName: RecallProfileName): string {
  let injection = `<palinode-memory profile="${profileName}">\n`;
  if (parts.coreContent) {
    // Capacity display line — inspired by NousResearch/hermes-agent prompt_builder.py
    // Lets the agent see how full core memory is and consolidate proactively
    const totalChars = parts.coreContent.length;
    const maxChars = parts.coreBudget ?? 8000;
    const pct = Math.round((totalChars / maxChars) * 100);
    const capacityLine = `[Core Memory: ${totalChars.toLocaleString()} / ${maxChars.toLocaleString()} chars — ${pct}%]\n\n`;
    injection += `## Core Memory\n${capacityLine}${parts.coreContent}\n`;
  }
  if (parts.topicContent) {
    injection += `\n## Relevant Context\n${parts.topicContent}\n`;
  }
  if (parts.assocContent) {
    injection += `\n## Associative Context\n${parts.assocContent}\n`;
  }
  if (parts.triggerContent) {
    injection += `\n## IMPORTANT Triggers\n${parts.triggerContent}\n`;
  }
  injection += "</palinode-memory>";
  return injection;
}

export function composeInjectionFrame(injection: string, systemRoleAvailable = true): InjectionFrame {
  if (systemRoleAvailable) {
    return { kind: "system", systemContext: injection };
  }
  return {
    kind: "fallback",
    prependContext:
      `<|reference_context|>\n${injection}\n<|end_reference_context|>\n\n` +
      "<|user_instruction_follows|>\n",
  };
}

// ============================================================================
// Helpers
// ============================================================================

function readFileIfExists(filePath: string): string | null {
  try {
    return fs.readFileSync(filePath, "utf-8");
  } catch {
    return null;
  }
}

function resolveWithin(baseDir: string, ...segments: string[]): string | null {
  const root = path.resolve(baseDir);
  const candidate = path.resolve(root, ...segments);
  const relative = path.relative(root, candidate);
  if (relative === "" || (!relative.startsWith("..") && !path.isAbsolute(relative))) {
    return candidate;
  }
  return null;
}

/** Load scrub patterns from specs/scrub-patterns.yaml and apply to text */
function scrubSensitive(text: string, palinodeDir: string): string {
  const scrubFile = resolveWithin(palinodeDir, "specs", "scrub-patterns.yaml");
  if (!scrubFile) return text;
  const raw = readFileIfExists(scrubFile);
  if (!raw) return text;

  let result = text;
  // Simple YAML pattern extraction (avoid adding a yaml dependency)
  const patternBlocks = raw.matchAll(/- pattern: '(.+?)'\s*\n\s*replace: '(.+?)'/g);
  for (const match of patternBlocks) {
    try {
      const regex = new RegExp(match[1], "g");
      result = result.replace(regex, match[2]);
    } catch {
      // Invalid regex — skip
    }
  }
  return result;
}

/** Check if a file's YAML frontmatter contains core: true */
function isCoreFile(content: string): boolean {
  const match = content.match(/^---\n([\s\S]*?)\n---/);
  if (!match) return false;
  return /^core:\s*true/m.test(match[1]);
}

async function palinodeFetch(
  baseUrl: string,
  endpoint: string,
  options?: RequestInit,
): Promise<any> {
  const res = await fetch(`${baseUrl}${endpoint}`, {
    ...options,
    headers: { "Content-Type": "application/json", ...options?.headers },
  });
  if (!res.ok) {
    throw new Error(`Palinode API ${endpoint}: ${res.status} ${res.statusText}`);
  }
  return res.json();
}

// ============================================================================
// Plugin
// ============================================================================

const palinodePlugin = {
  id: "openclaw-palinode",
  name: "Palinode Memory",
  description:
    "Persistent memory — files as truth, vectors as search. Injects core memory, extracts from conversations.",
  configSchema: palinodeConfigSchema,

  register(api: OpenClawPluginApi) {
    const cfg = palinodeConfigSchema.parse(api.pluginConfig);
    const promptsDir = resolveWithin(cfg.palinodeDir, cfg.promptsDir);
    if (!promptsDir) {
      throw new Error("palinode promptsDir must stay within palinodeDir");
    }

    api.logger.info(
      `openclaw-palinode: registered (api: ${cfg.palinodeApiUrl}, dir: ${cfg.palinodeDir}, autoRecall: ${cfg.autoRecall}, autoCapture: ${cfg.autoCapture})`,
    );

    // ========================================================================
    // Tools
    // ========================================================================

    api.registerTool(
      {
        name: "palinode_search",
        label: "Palinode Search",
        description:
          "Search Palinode memory for relevant past context, decisions, people, projects, or insights.",
        parameters: Type.Object({
          query: Type.String({ description: "Natural language search query" }),
          category: Type.Optional(
            Type.String({
              description:
                "Filter by category: person, project, decision, insight, research",
            }),
          ),
          limit: Type.Optional(
            Type.Number({ description: "Max results (default 5)" }),
          ),
          threshold: Type.Optional(
            Type.Number({ description: "Similarity threshold (0.0–1.0). Higher = stricter." }),
          ),
          since_days: Type.Optional(
            Type.Number({ description: "Only return memories from the last N days." }),
          ),
          types: Type.Optional(
            Type.Array(Type.String(), { description: "Filter by memory type (e.g. Decision, Insight)." }),
          ),
          date_after: Type.Optional(
            Type.String({ description: "Filter results after an ISO date (e.g. 2024-01-01)." }),
          ),
          date_before: Type.Optional(
            Type.String({ description: "Filter results before an ISO date." }),
          ),
          include_daily: Type.Optional(
            Type.Boolean({ description: "Include daily session notes in results (default false)." }),
          ),
          min_priority: Type.Optional(
            Type.Number({ description: "Only return memories with priority >= this value (1–5)." }),
          ),
          include_telemetry: Type.Optional(
            Type.Boolean({
              description:
                "Include machine/monitor telemetry writes (metadata.kind: telemetry). " +
                "Default false — telemetry is hard-excluded from recall so monitoring " +
                "churn does not pollute results (ADR-015 §5).",
            }),
          ),
        }),
        async execute(_toolCallId: string, params: any) {
          try {
            const body: Record<string, any> = {
              query: params.query,
              limit: params.limit || 5,
            };
            if (params.category !== undefined) body.category = params.category;
            if (params.threshold !== undefined) body.threshold = params.threshold;
            if (params.since_days !== undefined) body.since_days = params.since_days;
            if (params.types !== undefined) body.types = params.types;
            if (params.date_after !== undefined) body.date_after = params.date_after;
            if (params.date_before !== undefined) body.date_before = params.date_before;
            if (params.include_daily !== undefined) body.include_daily = params.include_daily;
            if (params.min_priority !== undefined) body.min_priority = params.min_priority;
            if (params.include_telemetry !== undefined) body.include_telemetry = params.include_telemetry;
            const results = await palinodeFetch(
              cfg.palinodeApiUrl,
              "/search",
              {
                method: "POST",
                body: JSON.stringify(body),
              },
            );

            if (!results || results.length === 0) {
              return {
                content: [
                  { type: "text", text: "No relevant memories found in Palinode." },
                ],
              };
            }

            const text = results
              .map(
                (r: any, i: number) =>
                  `${i + 1}. [${r.category || "?"}] ${r.content.slice(0, 200)}${r.content.length > 200 ? "..." : ""} (score: ${(r.score * 100).toFixed(0)}%, file: ${path.basename(r.file_path)})`,
              )
              .join("\n\n");

            return {
              content: [
                {
                  type: "text",
                  text: `Found ${results.length} memories:\n\n${text}`,
                },
              ],
            };
          } catch (err) {
            return {
              content: [
                {
                  type: "text",
                  text: `Palinode search failed: ${String(err)}`,
                },
              ],
            };
          }
        },
      },
      { name: "palinode_search" },
    );

    api.registerTool(
      {
        name: "palinode_save",
        label: "Palinode Save",
        description:
          "Save a memory to Palinode. Use for decisions, insights, person context, or project updates worth remembering across sessions.",
        parameters: Type.Object({
          content: Type.String({ description: "What to remember" }),
          type: Type.String({
            description:
              "Memory type: PersonMemory, Decision, ProjectSnapshot, Insight, ActionItem",
          }),
          entities: Type.Optional(
            Type.Array(Type.String(), {
              description:
                'Related entities, e.g. ["person/alice", "project/my-app"]',
            }),
          ),
          slug: Type.Optional(
            Type.String({
              description: "URL-safe filename slug (auto-generated if omitted)",
            }),
          ),
          core: Type.Optional(
            Type.Boolean({
              description: "If true, this memory is always injected at session start (core memory)",
            }),
          ),
          project: Type.Optional(
            Type.String({ description: "Project slug shorthand, e.g. 'palinode' becomes 'project/palinode'." }),
          ),
          metadata: Type.Optional(
            Type.Record(Type.String(), Type.Unknown(), { description: "Arbitrary additional frontmatter fields." }),
          ),
          confidence: Type.Optional(
            Type.Number({ description: "Confidence in this memory's accuracy (0.0–1.0)." }),
          ),
          external_refs: Type.Optional(
            Type.Record(Type.String(), Type.String(), {
              description:
                "SDLC object references. Recognised keys: gitlab_mr, gitlab_issue, gitlab_pipeline, github_pr, linear_issue, jira_issue. Free-form keys also accepted.",
            }),
          ),
          title: Type.Optional(
            Type.String({ description: "Optional human-readable title stored in frontmatter." }),
          ),
          source: Type.Optional(
            Type.String({ description: "Source surface that created this memory (e.g. 'claude-code'). Auto-detected if omitted." }),
          ),
          priority: Type.Optional(
            Type.Number({ description: "Memory priority (1–5). Higher values surface in priority-filtered recall." }),
          ),
          update_policy: Type.Optional(
            Type.Union(
              [Type.Literal("append"), Type.Literal("replace")],
              {
                description:
                  "Write-semantics axis (ADR-015). 'append' (default) is episodic. " +
                  "'replace' marks this as a living/current-state document: re-saving " +
                  "the same slug updates it in place and consolidation will never " +
                  "supersede/archive it into history. Persisted as sticky frontmatter.",
              },
            ),
          ),
          sources: Type.Optional(
            Type.Array(
              Type.Object({
                ref: Type.String({ description: "Path under the memory dir of the cited source." }),
                quote: Type.String({ description: "The exact passage cited from the source." }),
                quote_hash: Type.Optional(
                  Type.String({ description: "Integrity hash; computed on save if omitted." }),
                ),
              }),
              {
                description:
                  "Source-citation anchors (#459): each {ref, quote, quote_hash} " +
                  "anchors this memory to the exact passage it cites. quote_hash is " +
                  "computed server-side when omitted; the verifier reads these back.",
              },
            ),
          ),
        }),
        async execute(_toolCallId: string, params: any) {
          try {
            const result = await palinodeFetch(
              cfg.palinodeApiUrl,
              "/save",
              {
                method: "POST",
                body: JSON.stringify(params),
              },
            );

            return {
              content: [
                {
                  type: "text",
                  text: `Saved to Palinode: ${result.file_path} (${result.id})`,
                },
              ],
            };
          } catch (err) {
            return {
              content: [
                {
                  type: "text",
                  text: `Palinode save failed: ${String(err)}`,
                },
              ],
            };
          }
        },
      },
      { name: "palinode_save" },
    );

    api.registerTool(
      {
        name: "palinode_ingest",
        label: "Palinode Ingest",
        description:
          "Ingest a URL into Palinode research. Fetches the page, extracts content, saves as a research reference file.",
        parameters: Type.Object({
          url: Type.String({ description: "URL to fetch and ingest" }),
          name: Type.Optional(
            Type.String({ description: "Title/name for the reference (auto-generated if omitted)" }),
          ),
        }),
        async execute(_toolCallId: string, params: any) {
          try {
            const result = await palinodeFetch(cfg.palinodeApiUrl, "/ingest-url", {
              method: "POST",
              body: JSON.stringify({ url: params.url, name: params.name }),
            });
            if (result.file_path) {
              return {
                content: [{ type: "text", text: `Ingested to Palinode: ${result.file_path}` }],
              };
            }
            return {
              content: [{ type: "text", text: "URL fetched but no usable content extracted." }],
            };
          } catch (err) {
            return {
              content: [{ type: "text", text: `Palinode ingest failed: ${String(err)}` }],
            };
          }
        },
      },
      { name: "palinode_ingest" },
    );

    api.registerTool(
      {
        name: "palinode_status",
        label: "Palinode Status",
        description: "Show Palinode memory stats: file counts, index health, Ollama status.",
        parameters: Type.Object({}),
        async execute() {
          try {
            const stats = await palinodeFetch(cfg.palinodeApiUrl, "/status");
            return {
              content: [
                {
                  type: "text",
                  text: `Palinode: ${stats.total_files} files, ${stats.total_chunks} chunks indexed. Ollama: ${stats.ollama_reachable ? "✅" : "❌"}`,
                },
              ],
            };
          } catch (err) {
            return {
              content: [
                {
                  type: "text",
                  text: `Palinode status failed: ${String(err)}. Is the API server running?`,
                },
              ],
            };
          }
        },
      },
      { name: "palinode_status" },
    );

    api.registerTool({
        name: "palinode_diff",
        label: "Palinode Diff",
        description: "Show what memories changed recently.",
        parameters: Type.Object({
            days: Type.Optional(Type.Number({ description: "Look back N days (default 7)" })),
        }),
        async execute(_id: string, params: any) {
            const res = await palinodeFetch(cfg.palinodeApiUrl, `/diff?days=${params.days || 7}`);
            return { content: [{ type: "text", text: res.diff }] };
        },
    }, { name: "palinode_diff" });

    api.registerTool({
        name: "palinode_blame",
        label: "Palinode Blame",
        description: "Trace when a fact was recorded and by which session.",
        parameters: Type.Object({
            file: Type.String({ description: "Memory file path" }),
            search: Type.Optional(Type.String({ description: "Filter to matching lines" })),
        }),
        async execute(_id: string, params: any) {
            const query = params.search ? `?search=${encodeURIComponent(params.search)}` : "";
            const res = await palinodeFetch(cfg.palinodeApiUrl, `/blame/${params.file}${query}`);
            return { content: [{ type: "text", text: res.blame }] };
        },
    }, { name: "palinode_blame" });

    api.registerTool({
        name: "palinode_depends",
        label: "Palinode Depends",
        description: "Return the dependency tree for a milestone or task slug, or list all unblocked (ready-to-start) items.",
        parameters: Type.Object({
            slug: Type.Optional(Type.String({ description: "Milestone or task slug to inspect (e.g. 'milestone/M1'). Required unless unblocked=true." })),
            unblocked: Type.Optional(Type.Boolean({ description: "If true, return all slugs whose every depends_on is done. Default false." })),
        }),
        async execute(_id: string, params: any) {
            try {
                if (params.unblocked) {
                    const items = await palinodeFetch(cfg.palinodeApiUrl, "/depends/_unblocked");
                    if (!items || items.length === 0) {
                        return { content: [{ type: "text", text: "No unblocked items found." }] };
                    }
                    const lines = items.map((it: any) => `${it.slug}${it.status ? ` (${it.status})` : ""}`);
                    return { content: [{ type: "text", text: `Unblocked items:\n${lines.join("\n")}` }] };
                }
                if (!params.slug) {
                    return { content: [{ type: "text", text: "Error: 'slug' is required unless unblocked=true" }] };
                }
                const res = await palinodeFetch(cfg.palinodeApiUrl, `/depends/${encodeURIComponent(params.slug)}`);
                return { content: [{ type: "text", text: JSON.stringify(res, null, 2) }] };
            } catch (err) {
                return { content: [{ type: "text", text: `Palinode depends failed: ${String(err)}` }] };
            }
        },
    }, { name: "palinode_depends" });

    // ========================================================================
    // Quick-save flag: -es at end of message → save to Palinode
    // Works from any channel (Telegram, webchat, Discord, etc.)
    // ========================================================================
    
    let pendingEsReceipt: string | null = null;

    api.on("message_received", async (event: any) => {
      const content = event.content || event.context?.content || "";
      // Must end with -es (word boundary, not inside code/backticks)
      if (!/\s-es\s*$/.test(content) && content !== "-es") return;
      // Don't trigger inside code blocks
      if ((content.match(/```/g) || []).length % 2 === 1) return;

      // Strip the -es flag
      const textToSave = content.replace(/\s*-es\s*$/, "").trim();
      if (!textToSave || textToSave.length < 5) return;

      try {
        // Detect what kind of content this is
        const isJustUrl = /^https?:\/\/\S+$/.test(textToSave.trim());
        const isLong = textToSave.length > 500;

        if (isJustUrl) {
          // Pure URL → ingest it
          await palinodeFetch(cfg.palinodeApiUrl, "/ingest-url", {
            method: "POST",
            body: JSON.stringify({ url: textToSave.trim() }),
          });
          api.logger.info(`openclaw-palinode: -es ingested URL: ${textToSave.trim()}`);
          api.logger.info(`openclaw-palinode: -es saved (receipt suppressed — message_received hook is read-only)`);
        } else if (isLong) {
          // Long text (article, notes with citations) → save as research with source URLs extracted
          const urls = textToSave.match(/https?:\/\/\S+/g) || [];
          await palinodeFetch(cfg.palinodeApiUrl, "/save", {
            method: "POST",
            body: JSON.stringify({
              content: textToSave,
              type: "ResearchRef",
              metadata: {
                source_urls: urls,
                source_type: "pasted_article",
              },
            }),
          });
          api.logger.info(`openclaw-palinode: -es saved long content (${textToSave.length} chars, ${urls.length} URLs)`);
          pendingEsReceipt = `*[Saved to Palinode: Long ResearchRef with ${urls.length} URLs]*`;
        } else {
          // Short text — check for URLs
          const urls = textToSave.match(/https?:\/\/\S+/g) || [];
          await palinodeFetch(cfg.palinodeApiUrl, "/save", {
            method: "POST",
            body: JSON.stringify({
              content: textToSave,
              type: urls.length > 0 ? "ResearchRef" : "Insight",
              metadata: urls.length > 0 ? { source_urls: urls, source_type: "quick_capture" } : undefined,
            }),
          });

          // Also fetch any URLs found — capture the source alongside the note
          let fetched = 0;
          for (const url of urls) {
            try {
              await palinodeFetch(cfg.palinodeApiUrl, "/ingest-url", {
                method: "POST",
                body: JSON.stringify({ url }),
              });
              fetched++;
            } catch {
              // URL fetch failed — note was still saved, that's fine
            }
          }

          api.logger.info(`openclaw-palinode: -es quick-saved: "${textToSave.slice(0, 50)}..." (${urls.length} URLs found, ${fetched} fetched)`);
          let receipt = "*[Saved to Palinode";
          if (fetched > 0) receipt += ` — ${fetched} URL${fetched > 1 ? "s" : ""} also ingested`;
          else if (urls.length > 0) receipt += ` — ${urls.length} URL${urls.length > 1 ? "s" : ""} noted`;
          receipt += "]*";
          pendingEsReceipt = receipt;
        }
      } catch (err) {
        api.logger.warn(`openclaw-palinode: -es quick-save failed: ${String(err)}`);
      }
    });

    // ========================================================================
    // Auto-Recall: inject core memory before agent starts
    // ========================================================================

    if (cfg.autoRecall) {
      // Session turn counter — tracks when full core injection is needed.
      //
      // Core memory is injected in full:
      //   1. Turn 1 (session start) — model needs context from scratch
      //   2. After any compaction — context was summarized, injection was lost
      //
      // On all other turns, mid_turn_mode controls behaviour (default: "none" = skip core).
      // A periodic timer fallback (every N turns) is a last resort if compaction hooks fail.
      let sessionTurnCount = 0;
      let forceFullCoreNext = false; // Set to true after compaction

      // Hook: reset counter after compaction so next turn re-injects full core.
      // after_compaction is the correct SDK hook name (confirmed from types.d.ts).
      api.on("after_compaction", async () => {
        forceFullCoreNext = true;
        api.logger.info("openclaw-palinode: compaction detected — will re-inject full core on next turn");
      });

      api.on("before_prompt_build", async (event: any) => {
        if (!event.prompt || event.prompt.length < 3) return;

        sessionTurnCount++;
        const isFirstTurn = sessionTurnCount === 1;
        // Full core if: first turn, post-compaction, or every 200 turns as fallback
        const fullCoreThisTurn = isFirstTurn || forceFullCoreNext || (sessionTurnCount % 200 === 0);

        if (forceFullCoreNext) {
          forceFullCoreNext = false; // Consume the flag
        }

        // #391/#394: resolve the active recall profile up front. Empty sources
        // (off / minimal / autoRecall: false) short-circuit before any I/O.
        const profile = resolveProfile(cfg);
        if (profile.sources.length === 0) return;

        // Skip semantic search for trivial/short messages — just inject core
        const trivialMessage = event.prompt.trim().length < 15 ||
          /^(ok|yep|yay|hmm|hm|yes|no|sure|thanks|thx|cool|got it|nice|lol|k|👍|👎|✅|🙏)\.?$/i.test(event.prompt.trim());

        try {
          let coreContent = "";

          // Phase 1: Load all files with core: true
          // Gated by `profile.sources.includes("core")` (#391/#394) — profiles
          // like "monitoring" and "investigation" skip core entirely.
          // Within core-enabled profiles, midTurnMode still controls full vs
          // summary vs none for mid-turns.
          const CORE_FILE_MAX = profile.coreMaxCharsPerFile ?? 3000;
          const CORE_TOTAL_MAX = profile.coreBudget ?? 8000;
          const midTurnMode = cfg.midTurnMode || "none";
          const dirsToScan = ["people", "projects", "decisions", "insights"];
          let coreBudgetRemaining = CORE_TOTAL_MAX;

          const coreEnabled = profile.sources.includes("core");
          // On non-full turns with mode "none", skip core entirely.
          // Profile-disabled core also skips entirely.
          if (!coreEnabled || (!fullCoreThisTurn && midTurnMode === "none")) {
            // No core injection — the model still has turn 1's context (or core is profile-disabled)
          } else {
            for (const dir of dirsToScan) {
              const fullDir = resolveWithin(cfg.palinodeDir, dir);
              if (!fullDir) continue;
              if (!fs.existsSync(fullDir)) continue;
              const files = fs
                .readdirSync(fullDir)
                .filter((f: string) => f.endsWith(".md"));
              for (const file of files) {
                const filePath = resolveWithin(fullDir, file);
                if (!filePath) continue;
                const content = readFileIfExists(filePath);
                if (content && isCoreFile(content)) {
                  const summaryMatch = content.match(/^summary:\s*["']?(.+?)["']?\s*$/m);
                  const summary = summaryMatch ? summaryMatch[1].trim() : null;

                  if (!fullCoreThisTurn && midTurnMode === "summary") {
                    // Summary-only turns: inject one-liner (skip files without summary)
                    if (summary) {
                      coreContent += `\n--- ${dir}/${file} ---\n> ${summary}\n`;
                    }
                  } else {
                    // Full turn: inject content within budget
                    if (coreBudgetRemaining <= 0) continue; // Budget exhausted

                    let injected = content;
                    const maxForThis = Math.min(CORE_FILE_MAX, coreBudgetRemaining);
                    if (content.length > maxForThis) {
                      injected = content.slice(0, maxForThis) +
                        (summary
                          ? `\n...[truncated — summary: ${summary}]`
                          : `\n...[truncated — full file at ${dir}/${file}]`);
                    }
                    const header = summary ? `> ${summary}\n\n` : "";
                    const block = `\n--- ${dir}/${file} ---\n${header}${injected}\n`;
                    coreContent += block;
                    coreBudgetRemaining -= block.length;
                  }
                }
              }
            }
          }

          // Phase 2: Topic-specific retrieval based on the user's message.
          // Gated by `profile.sources.includes("semantic")` (#391/#394).
          // Skip for trivial messages — no value in searching for "ok" or "yay".
          let topicContent = "";
          if (!trivialMessage && profile.sources.includes("semantic")) {
            try {
              const semBody: Record<string, unknown> = {
                query: event.prompt,
                limit: profile.semanticLimit ?? 5,
              };
              // Forward type filters defensively — older Palinode servers ignore unknown keys.
              if (profile.typeAllow) semBody.type_allow = profile.typeAllow;
              if (profile.typeDeny)  semBody.type_deny  = profile.typeDeny;
              const results = await palinodeFetch(cfg.palinodeApiUrl, "/search", {
                method: "POST",
                body: JSON.stringify(semBody),
              });
              if (results && results.length > 0) {
                const cap = profile.semanticMaxChars ?? 700;
                topicContent = results
                  .map((r: any) => {
                    // Prefer server-side query-windowed snippet (#359/#392).
                    // Fall back to blunt slice for older servers / empty snippets.
                    const body = ((r.snippet ?? "") as string).trim() ||
                                 ((r.content ?? "") as string).slice(0, cap);
                    return `[${r.category || "memory"}] ${body}`;
                  })
                  .join("\n\n");
              }
            } catch {
              // Search unavailable — degrade gracefully
            }
          }

          // Phase 3: Associative Recall (Entity Graph)
          // Gated by `profile.sources.includes("associative")` (#391/#394).
          // Note: prior to #392/#393, /search-associative returned un-truncated
          // content. The snippet-preference fallback covers older servers.
          let assocContent = "";
          if (!trivialMessage && profile.sources.includes("associative")) {
            try {
              const assocBody: Record<string, unknown> = {
                query: event.prompt,
                seed_entities: [],
                limit: profile.associativeLimit ?? 3,
              };
              if (profile.typeAllow) assocBody.type_allow = profile.typeAllow;
              if (profile.typeDeny)  assocBody.type_deny  = profile.typeDeny;
              const assoc = await palinodeFetch(cfg.palinodeApiUrl, "/search-associative", {
                  method: "POST",
                  body: JSON.stringify(assocBody),
              });
              if (assoc && assoc.length > 0) {
                  const cap = profile.associativeMaxChars ?? 500;
                  assocContent = assoc.map((r: any) => {
                    const body = ((r.snippet ?? "") as string).trim() ||
                                 ((r.content ?? "") as string).slice(0, cap);
                    return `[Related Context via Entity Graph] File: ${r.file_path}\n${body}`;
                  }).join("\n\n");
              }
            } catch {
              // degrade gracefully
            }
          }

          // Phase 4: Prospective Triggers
          // Gated by `profile.sources.includes("triggers")` (#391/#394).
          // Trigger count + per-trigger char cap are profile-driven.
          let triggerContent = "";
          if (!trivialMessage && profile.sources.includes("triggers")) {
            try {
              const triggers = await palinodeFetch(cfg.palinodeApiUrl, "/check-triggers", {
                  method: "POST",
                  body: JSON.stringify({ query: event.prompt })
              });
              if (triggers && triggers.length > 0) {
                  const nMax = profile.triggersLimit ?? 10;
                  const triggerCap = profile.triggersMaxCharsEach ?? 2000;
                  triggerContent = triggers.slice(0, nMax).map((t: any) => {
                      const triggerPath = resolveWithin(cfg.palinodeDir, t.memory_file);
                      if (!triggerPath) {
                          return `\n[TRIGGER SKIPPED: ${t.description}] -> Unsafe path: ${t.memory_file}`;
                      }
                      const content = readFileIfExists(triggerPath);
                      if (content) {
                          return `\n\n--- Triggered: ${t.description} (${t.memory_file}) ---\n${content.slice(0, triggerCap)}`;
                      } else {
                          return `\n[TRIGGER FIRED: ${t.description}] -> File: ${t.memory_file} (file not found)`;
                      }
                  }).join("\n\n");
              }
            } catch {
              // degrade gracefully
            }
          }

          // Nothing to inject — bail before scrubbing/logging.
          if (!coreContent && !topicContent && !assocContent && !triggerContent) {
            return;
          }

          let injection = composeInjection(
            {
              coreContent,
              topicContent,
              assocContent,
              triggerContent,
              coreBudget: CORE_TOTAL_MAX,
            },
            cfg.recallProfile,
          );

          // #391/#394: enforce total-budget hard cap across all sources.
          // Prevents pathological compound growth — observed monitor prompt
          // class: 69K tokens from a single autoRecall firing on a 200-token
          // cron prompt.
          if (profile.totalBudget && injection.length > profile.totalBudget) {
            injection = injection.slice(0, profile.totalBudget) +
              `\n…[truncated by recallProfile total budget: ${profile.totalBudget} chars]\n</palinode-memory>`;
          }

          // Scrub sensitive content before injection
          injection = scrubSensitive(injection, cfg.palinodeDir);

          api.logger.info(
            `openclaw-palinode: turn ${sessionTurnCount} — profile="${cfg.recallProfile}" ` +
            `sources=[${profile.sources.join(",")}] ` +
            `${fullCoreThisTurn ? "FULL core" : "summary-only"} + ${topicContent ? "topic search" : "no search"} ` +
            `(${injection.length} chars)`,
          );

          if (pendingEsReceipt) {
            injection = pendingEsReceipt + "\n\n" + injection;
            pendingEsReceipt = null;
          }

          return { systemContext: injection };
        } catch (err) {
          api.logger.warn(`openclaw-palinode: recall failed: ${String(err)}`);
        }
      });
    }

    // ========================================================================
    // Auto-Capture: extract memories after agent ends
    // ========================================================================

    if (cfg.autoCapture) {
      api.on("agent_end", async (event: any) => {
        if (!event.success || !event.messages || event.messages.length === 0) {
          return;
        }

        try {
          // Read PROGRAM.md for behavior instructions
          const programContent = readFileIfExists(
            resolveWithin(cfg.palinodeDir, "PROGRAM.md") ?? "",
          );

          // Read extraction prompt
          const extractionPrompt = readFileIfExists(
            resolveWithin(promptsDir, "extraction.md") ?? "",
          );

          if (!extractionPrompt) {
            api.logger.warn(
              "openclaw-palinode: extraction prompt not found, skipping auto-capture",
            );
            return;
          }

          // Build the messages for extraction
          const recentMessages = event.messages.slice(-10);
          const formattedMessages: string[] = [];

          for (const msg of recentMessages) {
            if (!msg || typeof msg !== "object") continue;
            const role = (msg as any).role;
            if (role !== "user" && role !== "assistant") continue;

            let textContent = "";
            const content = (msg as any).content;
            if (typeof content === "string") {
              textContent = content;
            } else if (Array.isArray(content)) {
              for (const block of content) {
                if (block?.text && typeof block.text === "string") {
                  textContent += (textContent ? "\n" : "") + block.text;
                }
              }
            }

            if (!textContent) continue;
            // Strip injected palinode context to avoid feedback loop
            textContent = textContent
              .replace(/<palinode-memory>[\s\S]*?<\/palinode-memory>\s*/g, "")
              .trim();
            if (!textContent) continue;

            formattedMessages.push(`${role}: ${textContent}`);
          }

          if (formattedMessages.length === 0) return;

          const conversationText = formattedMessages.join("\n\n");

          // We can't call an LLM directly from the plugin (no api.llm).
          // Instead, save the session summary to daily/ for the memory manager
          // to process later, OR use the Palinode API /save for simple captures.
          //
          // For MVP: save a session summary to daily/
          const today = new Date().toISOString().split("T")[0];
          const dailyPath = resolveWithin(cfg.palinodeDir, "daily", `${today}.md`);
          if (!dailyPath) {
            api.logger.warn("openclaw-palinode: unsafe daily path during auto-capture");
            return;
          }
          const dirPath = path.dirname(dailyPath);
          if (!fs.existsSync(dirPath)) {
            fs.mkdirSync(dirPath, { recursive: true });
          }

          // Append to today's daily note
          const sessionSummary = `\n\n## Session ${new Date().toISOString()}\n\n${conversationText.slice(0, 2000)}\n`;

          fs.appendFileSync(dailyPath, sessionSummary);

          api.logger.info(
            `openclaw-palinode: appended session to daily/${today}.md (${formattedMessages.length} messages)`,
          );

          // Tier 1: Session-end status append
          // For long sessions: extract unique TOPICS discussed, not just one line.
          // For short sessions: capture intent → result.
          try {
            const userMessages = formattedMessages
              .filter((m: string) => m.startsWith("user:"))
              .map((m: string) => m.replace(/^user:\s*/, "").replace(/\n.*/s, "").trim())
              .filter((l: string) => l.length > 15);

            const assistantMessages = formattedMessages
              .filter((m: string) => m.startsWith("A:"))
              .map((m: string) => m.replace(/^A:\s*/, "").replace(/\n.*/s, "").trim())
              .filter((l: string) => l.length > 20);

            let summary = "";
            const isLongSession = formattedMessages.length > 20;

            if (isLongSession) {
              // Long session: sample user messages across the session to capture topics
              // Take first, middle, and last user messages for breadth
              const sample: string[] = [];
              if (userMessages.length > 0) sample.push(userMessages[0]);
              if (userMessages.length > 4) sample.push(userMessages[Math.floor(userMessages.length / 2)]);
              if (userMessages.length > 2) sample.push(userMessages[userMessages.length - 1]);
              // Deduplicate and truncate
              const unique = [...new Set(sample)].map((s: string) => s.slice(0, 60));
              summary = `[${formattedMessages.length} msgs] ${unique.join("; ")}`;
            } else {
              // Short session: intent → result
              const intent = userMessages[0] || "";
              const result = assistantMessages[assistantMessages.length - 1] || "";
              if (intent && result) {
                summary = `${intent.slice(0, 100)} → ${result.slice(0, 100)}`;
              } else {
                summary = (result || intent).slice(0, 200);
              }
            }

            if (summary.length > 15) {

              // Use entity detection API to find which project was discussed
              try {
                const detectRes = await fetch(`${cfg.palinodeApiUrl}/entities`, {
                  signal: AbortSignal.timeout(3000),
                });
                if (detectRes.ok) {
                  const entities: any[] = await detectRes.json();
                  // Find project entities that have status files
                  const projectEntities = entities
                    .filter((e: any) => e.entity_ref?.startsWith("project/"))
                    .map((e: any) => e.entity_ref.replace("project/", ""));

                  const statusDir = path.join(cfg.palinodeDir, "projects");
                  if (fs.existsSync(statusDir)) {
                    for (const proj of projectEntities) {
                      const sfPath = path.join(statusDir, `${proj}-status.md`);
                      if (fs.existsSync(sfPath)) {
                        // Check if this session mentioned this project
                        const projLower = proj.toLowerCase().replace(/-/g, " ");
                        const mentioned = conversationText.toLowerCase().includes(projLower);
                        if (mentioned) {
                          fs.appendFileSync(sfPath, `\n- [${today}] ${summary}\n`);
                          api.logger.info(`openclaw-palinode: status append → ${proj}-status.md`);
                        }
                      }
                    }
                  }
                }
              } catch {
                // API not reachable — skip status append silently
              }
            }
          } catch (statusErr) {
            api.logger.warn(
              `openclaw-palinode: status append failed: ${String(statusErr)}`,
            );
          }
        } catch (err) {
          api.logger.warn(`openclaw-palinode: capture failed: ${String(err)}`);
        }
      });
    }

    // ========================================================================
    // CLI
    // ========================================================================

    api.registerCli(
      ({ program }: any) => {
        const palinode = program
          .command("palinode")
          .description("Palinode memory commands");

        palinode
          .command("search")
          .description("Search Palinode memory")
          .argument("<query>", "Search query")
          .option("--limit <n>", "Max results", "5")
          .option("--category <cat>", "Filter by category")
          .action(async (query: string, opts: any) => {
            try {
              const results = await palinodeFetch(cfg.palinodeApiUrl, "/search", {
                method: "POST",
                body: JSON.stringify({
                  query,
                  limit: parseInt(opts.limit, 10),
                  category: opts.category,
                }),
              });
              console.log(JSON.stringify(results, null, 2));
            } catch (err) {
              console.error(`Search failed: ${String(err)}`);
            }
          });

        palinode
          .command("stats")
          .description("Show Palinode statistics")
          .action(async () => {
            try {
              const stats = await palinodeFetch(cfg.palinodeApiUrl, "/status");
              console.log(JSON.stringify(stats, null, 2));
            } catch (err) {
              console.error(`Stats failed: ${String(err)}`);
            }
          });

        palinode
          .command("reindex")
          .description("Rebuild the Palinode vector index from files")
          .action(async () => {
            try {
              const result = await palinodeFetch(cfg.palinodeApiUrl, "/reindex", {
                method: "POST",
              });
              console.log(JSON.stringify(result, null, 2));
            } catch (err) {
              console.error(`Reindex failed: ${String(err)}`);
            }
          });
      },
      { commands: ["palinode"] },
    );

    // ========================================================================
    // /new command hook — flush session summary before context resets
    // ========================================================================

    api.on("before_reset", async (event: any) => {
      try {
        const messages = event.messages || [];
        if (messages.length === 0) return;

        const formattedMessages: string[] = [];
        for (const msg of messages.slice(-20)) {
          if (!msg || typeof msg !== "object") continue;
          const role = (msg as any).role;
          if (role !== "user" && role !== "assistant") continue;
          let textContent = "";
          const content = (msg as any).content;
          if (typeof content === "string") {
            textContent = content;
          } else if (Array.isArray(content)) {
            for (const block of content) {
              if (block?.text) textContent += (textContent ? "\n" : "") + block.text;
            }
          }
          textContent = textContent.replace(/<palinode-memory>[\s\S]*?<\/palinode-memory>\s*/g, "").trim();
          if (!textContent) continue;
          formattedMessages.push(`${role}: ${textContent.slice(0, 500)}`);
        }

        if (formattedMessages.length === 0) return;

        const today = new Date().toISOString().split("T")[0];
        const dailyPath = resolveWithin(cfg.palinodeDir, "daily", `${today}.md`);
        if (!dailyPath) {
          api.logger.warn("openclaw-palinode: unsafe daily path during /new flush");
          return;
        }
        const sessionSummary = `\n\n## /new flush — ${new Date().toISOString()}\n\n${formattedMessages.join("\n\n").slice(0, 3000)}\n`;

        const existing = readFileIfExists(dailyPath) || `# Daily Notes — ${today}\n`;
        fs.writeFileSync(dailyPath, existing + sessionSummary, "utf-8");
        api.logger.info(`openclaw-palinode: /new hook — flushed ${formattedMessages.length} messages to ${dailyPath}`);
      } catch (err) {
        api.logger.warn(`openclaw-palinode: /new hook failed: ${String(err)}`);
      }
    });

    // ========================================================================
    // Service
    // ========================================================================

    api.registerService({
      id: "openclaw-palinode",
      start: () => {
        api.logger.info(
          `openclaw-palinode: initialized (autoRecall: ${cfg.autoRecall}, autoCapture: ${cfg.autoCapture})`,
        );
      },
      stop: () => {
        api.logger.info("openclaw-palinode: stopped");
      },
    });
  },
};

export default palinodePlugin;
