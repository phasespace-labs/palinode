/**
 * OpenClaw Palinode Plugin
 *
 * Persistent memory system — files as truth, vectors as search.
 * Connects to the Palinode Python API for search/save operations.
 * Injects core memory at session start, extracts memories at session end.
 *
 * Host-side install / opt-in flags: see plugin/INSTALL.md
 */

import * as path from "path";
import { Type } from "@sinclair/typebox";
import type { OpenClawPluginApi } from "openclaw/plugin-sdk";

// Keep these literals aligned with palinode/core/parity.py. The plugin cannot
// import Python; tests/test_surface_parity.py enforces exact cross-language drift.
const PALINODE_CATEGORIES = [
  "people",
  "projects",
  "decisions",
  "insights",
  "research",
] as const;
const PALINODE_MEMORY_TYPES = [
  "PersonMemory",
  "Decision",
  "ProjectSnapshot",
  "Insight",
  "ResearchRef",
  "ActionItem",
] as const;
const PALINODE_TIERS = [
  "abstract",
  "overview",
  "full",
] as const;
const PALINODE_RESOLVE_MODES = [
  "none",
  "linked",
  "full",
] as const;

function literalUnion(
  values: readonly string[],
  options?: { description?: string },
) {
  return Type.Union(values.map((value) => Type.Literal(value)), options);
}

// ============================================================================
// Config
// ============================================================================

const DEFAULTS = {
  palinodeApiUrl: "http://localhost:6340",
  palinodeDir: path.join(process.env.HOME || "", "palinode"),
  promptsDir: "specs/prompts",
  autoCapture: false,
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

/** `{ type: "string", enum: [...] }` — the JSON Schema shape OpenClaw's bundled manifests use. */
function stringEnum<T extends string>(values: readonly T[], description: string) {
  return Type.Unsafe<T>({ type: "string", enum: [...values], description });
}

const MID_TURN_MODES = ["none", "summary", "full"] as const;

const RECALL_SOURCES = ["core", "semantic", "associative", "triggers"] as const satisfies readonly RecallSource[];

const recallProfileConfigSchema = Type.Object(
  {
    sources: Type.Optional(Type.Array(stringEnum(RECALL_SOURCES, "Recall source"))),
    coreMaxCharsPerFile: Type.Optional(Type.Number()),
    coreBudget: Type.Optional(Type.Number()),
    semanticLimit: Type.Optional(Type.Number()),
    semanticMaxChars: Type.Optional(Type.Number()),
    associativeLimit: Type.Optional(Type.Number()),
    associativeMaxChars: Type.Optional(Type.Number()),
    triggersLimit: Type.Optional(Type.Number()),
    triggersMaxCharsEach: Type.Optional(Type.Number()),
    typeAllow: Type.Optional(Type.Array(Type.String())),
    typeDeny: Type.Optional(Type.Array(Type.String())),
    totalBudget: Type.Optional(Type.Number()),
  },
  {
    additionalProperties: false,
    description: "Per-field overrides applied on top of the named recall preset",
  },
);

/**
 * The single source of truth for the plugin's config surface.
 *
 * `openclaw.plugin.json`'s `configSchema` is GENERATED from this object
 * (`npm run manifest`; `test/manifest.test.ts` fails when the committed copy
 * drifts). The OpenClaw host validates `plugins.entries.<id>.config` against
 * the manifest copy with Ajv before the plugin module is even loaded, so a key
 * accepted by `parse()` below but missing here is rejected at load time.
 *
 * Every property is optional and none carries a `default`: `parse()` owns the
 * defaults, and `palinodeDir`'s default is resolved from `$HOME` at runtime —
 * it cannot be a static string in a manifest, and the host applies manifest
 * defaults (`useDefaults`) to the user's config.
 */
export const PALINODE_CONFIG_SCHEMA = Type.Object(
  {
    palinodeApiUrl: Type.Optional(Type.String({ description: "Palinode API server URL" })),
    palinodeDir: Type.Optional(Type.String({ description: "Path to the Palinode memory directory" })),
    promptsDir: Type.Optional(
      Type.String({ description: "Path to extraction prompts, relative to palinodeDir" }),
    ),
    autoCapture: Type.Optional(
      Type.Boolean({ description: "Opt in to bounded transcript capture through the API at agent end and reset" }),
    ),
    autoRecall: Type.Optional(
      Type.Boolean({ description: "Inject core memory + semantic recall before each agent turn" }),
    ),
    midTurnMode: Type.Optional(
      stringEnum(MID_TURN_MODES, "Core-memory injection on turns after the first: none, summary lines, or full files"),
    ),
    recallProfile: Type.Optional(
      stringEnum(Object.keys(PROFILES) as RecallProfileName[], "Named recall preset"),
    ),
    recallProfileConfig: Type.Optional(recallProfileConfigSchema),
  },
  { additionalProperties: false },
);

const palinodeConfigSchema = {
  ...PALINODE_CONFIG_SCHEMA,
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
      autoCapture: cfg.autoCapture === true,
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

function resolveWithin(baseDir: string, ...segments: string[]): string | null {
  const root = path.resolve(baseDir);
  const candidate = path.resolve(root, ...segments);
  const relative = path.relative(root, candidate);
  if (relative === "" || (!relative.startsWith("..") && !path.isAbsolute(relative))) {
    return candidate;
  }
  return null;
}

// Reads the body whatever the status. Callers that only want the happy path
// use `palinodeFetch` below; the one caller that needs a non-2xx payload — a
// correction refusal or a partial apply, where the payload IS the answer —
// uses this and decides for itself.
async function palinodeFetchWithStatus(
  baseUrl: string,
  endpoint: string,
  options?: RequestInit,
): Promise<{ ok: boolean; status: number; statusText: string; body: any }> {
  const headers = new Headers(options?.headers);
  headers.set("Content-Type", "application/json");
  // Same deployment credential as the shared Pi/Cline client.
  const token = process.env.PALINODE_API_TOKEN?.trim();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const res = await fetch(`${baseUrl}${endpoint}`, {
    ...options,
    headers,
  });
  let body: any = null;
  try {
    body = await res.json();
  } catch {
    body = null;
  }
  return { ok: res.ok, status: res.status, statusText: res.statusText, body };
}

async function palinodeFetch(
  baseUrl: string,
  endpoint: string,
  options?: RequestInit,
): Promise<any> {
  const res = await palinodeFetchWithStatus(baseUrl, endpoint, options);
  if (!res.ok) {
    throw new Error(`Palinode API ${endpoint}: ${res.status} ${res.statusText}`);
  }
  return res.body;
}

type AutomaticScope = { cwd?: string; project?: string };

/** A hit the caller asked for across projects carries its own project(s). */
function otherProjectLabel(r: any): string {
  return Array.isArray(r?.other_project) && r.other_project.length
    ? ` [other project: ${r.other_project.join(", ")}]`
    : "";
}

function automaticScope(event: any, context: any): AutomaticScope {
  const cwd = context?.workspaceDir ?? event?.cwd;
  return typeof cwd === "string" && path.isAbsolute(cwd) ? { cwd } : {};
}

async function automaticCheck(
  baseUrl: string, action: "capture" | "recall", scope: AutomaticScope,
): Promise<{ allowed: boolean; project: string | null }> {
  if (!scope.cwd) return { allowed: false, project: null };
  try {
    const result = await palinodeFetch(baseUrl, "/controls/check", {
      method: "POST", signal: AbortSignal.timeout(3000),
      body: JSON.stringify({ action, automatic: true, ...scope }),
    });
    return { allowed: result?.allowed === true, project: resolvedProject(result) };
  } catch {
    return { allowed: false, project: null };
  }
}

function resolvedProject(result: any): string | null {
  return typeof result?.project === "string" && result.project.trim()
    ? result.project.trim().replace(/^project\//, "") : null;
}

async function automaticAllowed(
  baseUrl: string, action: "capture" | "recall", scope: AutomaticScope,
): Promise<boolean> {
  return (await automaticCheck(baseUrl, action, scope)).allowed;
}

async function resolveClientProject(baseUrl: string, scope: AutomaticScope): Promise<string | null> {
  if (!scope.cwd) return null;
  // Only the automatic controls check resolves cwd. This is a scope lookup;
  // the explicit /search request still applies its own recall policy.
  const result = await palinodeFetch(baseUrl, "/controls/check", {
    method: "POST", signal: AbortSignal.timeout(3000),
    body: JSON.stringify({ action: "recall", automatic: true, ...scope }),
  });
  return resolvedProject(result);
}

// These endpoints have no context field. Apply project isolation using the
// server's live listing before including content, paths, or trigger descriptions.
function belongsToProject(file: any, project: string | null): boolean {
  if (!project) return true;
  const raw = file?.entities;
  const entities = typeof raw === "string" ? [raw] : Array.isArray(raw) ? raw : [];
  const projects = entities.filter((ref: unknown): ref is string =>
    typeof ref === "string" && ref.trim().toLowerCase().startsWith("project/"),
  ).map((ref: string) => ref.trim().toLowerCase());
  return projects.length === 0 || projects.includes(`project/${project.toLowerCase()}`);
}

/** Keep aligned with palinode/core/scoring.py; OpenClaw has not migrated to the shared core yet. */
function describeMatch(result: { score?: number; raw_score?: number | null }): string {
  const rank = (result.score ?? 0).toFixed(2);
  if (result.raw_score === null) return `keyword match, rank ${rank}`;
  if (typeof result.raw_score === "number") {
    return `${Math.round(result.raw_score * 100)}% match`;
  }
  return `rank ${rank}`;
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
      (toolContext: { workspaceDir?: string }) => ({
        name: "palinode_search",
        label: "Palinode Search",
        description:
          "Search Palinode memory for relevant past context, decisions, people, projects, or insights.",
        parameters: Type.Object({
          query: Type.String({ description: "Natural language search query" }),
          category: Type.Optional(
            literalUnion(PALINODE_CATEGORIES, {
              description:
                `Filter by category: ${PALINODE_CATEGORIES.join(", ")}`,
            }),
          ),
          limit: Type.Optional(
            Type.Number({ description: "Max results (default 5)" }),
          ),
          threshold: Type.Optional(
            Type.Number({ description: "Vector similarity floor (0.0–1.0); ignored in lexical mode." }),
          ),
          since_days: Type.Optional(
            Type.Number({ description: "Only return memories from the last N days." }),
          ),
          types: Type.Optional(
            Type.Array(literalUnion(PALINODE_MEMORY_TYPES), {
              description: "Filter by memory type (e.g. Decision, Insight).",
            }),
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
          tier: Type.Optional(
            literalUnion(PALINODE_TIERS, {
              description:
                "How much of each hit to return: abstract (~300 chars, summary " +
                "first), overview (frontmatter + head of body), or full. Omit " +
                "for the default snippet view.",
            }),
          ),
          resolve: Type.Optional(
            literalUnion(PALINODE_RESOLVE_MODES, {
              description:
                "Attach evidence around each hit: linked (follow superseded_by / " +
                "contradicts / backed_by both ways under fixed budgets) or full " +
                "(also bounded unlinked discovery). Each hit reports coverage. " +
                "Default none.",
            }),
          ),
          include_other_projects: Type.Optional(
            Type.Boolean({
              description:
                "Also return memories tagged to other projects, each labelled " +
                "with its project. Default false: a project-scoped search leaves " +
                "them out.",
            }),
          ),
          include_retired: Type.Optional(
            Type.Boolean({
              description:
                "With resolve: also show retired records (archived, superseded, " +
                "retracted, expired) in each hit's evidence, labelled. Default false.",
            }),
          ),
        }),
        async execute(_toolCallId: string, params: any) {
          try {
            const body: Record<string, any> = {
              query: params.query,
              limit: params.limit || 5,
            };
            const project = await resolveClientProject(
              cfg.palinodeApiUrl, automaticScope(undefined, toolContext),
            );
            if (project) body.context = [`project/${project}`];
            if (params.category !== undefined) body.category = params.category;
            if (params.threshold !== undefined) body.threshold = params.threshold;
            if (params.since_days !== undefined) body.since_days = params.since_days;
            if (params.types !== undefined) body.types = params.types;
            if (params.date_after !== undefined) body.date_after = params.date_after;
            if (params.date_before !== undefined) body.date_before = params.date_before;
            if (params.include_daily !== undefined) body.include_daily = params.include_daily;
            if (params.min_priority !== undefined) body.min_priority = params.min_priority;
            if (params.include_telemetry !== undefined) body.include_telemetry = params.include_telemetry;
            if (params.tier !== undefined) body.tier = params.tier;
            if (params.resolve !== undefined && params.resolve !== "none") body.resolve = params.resolve;
            if (params.include_other_projects) body.include_other_projects = true;
            if (params.include_retired === true) body.include_retired = true;
            body.receipt = true;
            const payload = await palinodeFetch(
              cfg.palinodeApiUrl,
              "/search",
              {
                method: "POST",
                body: JSON.stringify(body),
              },
            );

            const results = Array.isArray(payload) ? payload : (payload?.results ?? []);
            const retrieval = payload?.receipt?.retrieval;
            // Which project the server scoped this delivery to, and the source
            // that decided it (an argument, the pinned setting, git, or none).
            // The server resolves it — this surface never re-derives scope.
            const scope = Array.isArray(payload) || !payload?.project_resolved_by
              ? ""
              : `Scope: ${payload.project ?? "none"} (${payload.project_resolved_by})\n`;
            // `outcome` says whether rows came back; `confidence` says whether
            // any of them is worth treating as an answer, which the delivered
            // score cannot say because it is a fused rank. A `none` verdict
            // leads the text — the results are still listed, never withheld.
            const verdict = retrieval?.confidence
              ? ` · match confidence: ${retrieval.confidence}`
              : "";
            // A scoped search that isolation left (nearly) empty says so, in
            // the MCP surface's agent-facing words: no option name, because an
            // agent told how to see another project's records fetches them and
            // answers with them. Quiet otherwise.
            const otherWithheld = Number(retrieval?.other_projects_withheld ?? 0);
            const scopeRef = String(payload?.project ?? "none");
            const withheldLine = otherWithheld >= 1 && (results?.length ?? 0) < 2
              ? `${otherWithheld} ${otherWithheld === 1 ? "record" : "records"} from other projects withheld ` +
                `(scope: ${scopeRef.startsWith("project/") ? scopeRef : `project/${scopeRef}`}). ` +
                "They are about other projects, not this one.\n"
              : "";
            const diagnostic = scope + withheldLine + (retrieval
              ? `Retrieval: ${retrieval.active_mode} · index: ${retrieval.index_state} · ${retrieval.outcome}${verdict}\n`
              : "") + (retrieval?.confidence === "none"
              ? "No confident match — the results below are the closest weak matches.\n"
              : "");

            if (!results || results.length === 0) {
              return {
                content: [
                  { type: "text", text: diagnostic + "No relevant memories found in Palinode." },
                ],
              };
            }

            const text = results
              .map(
                (r: any, i: number) =>
                  `${i + 1}. [${r.category || "?"}]${otherProjectLabel(r)} ${r.content.slice(0, 200)}${r.content.length > 200 ? "..." : ""} (${describeMatch(r)}, file: ${path.basename(r.file_path)})`,
              )
              .join("\n\n");

            return {
              content: [
                {
                  type: "text",
                  text: `${diagnostic}Found ${results.length} memories:\n\n${text}`,
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
      }),
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
          type: literalUnion(PALINODE_MEMORY_TYPES, {
            description:
              `Memory type: ${PALINODE_MEMORY_TYPES.join(", ")}`,
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
          epistemic: Type.Optional(
            Type.Union(
              [
                Type.Literal("fact"),
                Type.Literal("inference"),
                Type.Literal("unverified"),
                Type.Literal("open_question"),
              ],
              {
                description:
                  "Epistemic marker (ADR-018): the KIND of claim this memory makes. " +
                  "'fact' (observed/verified), 'inference' (derived, lower trust), " +
                  "'unverified' (asserted but not checked), or 'open_question' " +
                  "(unresolved). Omit to leave the memory " +
                  "unmarked (no claim — not treated as fact); persisted as " +
                  "frontmatter only when set.",
              },
            ),
          ),
          update_policy: Type.Optional(
            Type.Union(
              [Type.Literal("append"), Type.Literal("replace")],
              {
                description:
                  "How a save to an EXISTING slug is written (ADR-015). 'append' adds " +
                  "to the document: the existing body is kept and this content lands " +
                  "under a dated heading beneath it. 'replace' overwrites the body and " +
                  "marks this a living/current-state document that consolidation will " +
                  "never supersede/archive into history. Omit and the save overwrites " +
                  "without marking anything. Persisted as sticky frontmatter.",
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
          claims: Type.Optional(
            Type.Array(
              Type.Object({
                claim_id: Type.Optional(
                  Type.String({ description: "Content-addressed claim identifier; derived when omitted." }),
                ),
                text: Type.String({ description: "The claim being supported by the cited span." }),
                source_id: Type.String({ description: "Path under the memory dir of the cited source." }),
                span: Type.Object({
                  quote: Type.String({ description: "The exact source passage supporting the claim." }),
                  quote_hash: Type.Optional(
                    Type.String({ description: "Integrity hash; computed on save if omitted." }),
                  ),
                }),
                anchor_id: Type.Optional(
                  Type.String({ description: "Optional external anchor identifier carried verbatim." }),
                ),
              }),
              {
                description:
                  "Claim-level source anchors: each claim binds its text to an exact source span.",
              },
            ),
          ),
          contradicts: Type.Optional(
            Type.Array(Type.String(), {
              description:
                "Typed conflict links (#533): refs (category/slug) this memory " +
                "conflicts with. Records the conflict WITHOUT picking a winner " +
                "(that is supersession's job); surfaced by `palinode lint`.",
            }),
          ),
          backed_by: Type.Optional(
            Type.Array(Type.String(), {
              description:
                "Typed evidence links (#533): refs (category/slug) that " +
                "support/back this memory.",
            }),
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

            // An append is a different act from an overwrite, so the receipt
            // says which one happened. The MCP and CLI receipts do the same:
            // a caller who asked for `update_policy: append` must be able to
            // tell from the reply whether their prior body survived.
            const verb =
              result.save_outcome === "appended"
                ? "Appended to Palinode"
                : "Saved to Palinode";
            return {
              content: [
                {
                  type: "text",
                  text: `${verb}: ${result.file_path} (${result.id})`,
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
        name: "palinode_explain",
        label: "Palinode Explain Delivery",
        description: "Explain one delivery of context: given the bundle_id a search receipt returned, show which memories were supplied, the exact revision of each (and whether its source changed since), the resolved scope, the calling surface, each record's disposition and the coverage qualifiers. Fields that were never recorded are reported as unavailable with the reason, never guessed. Supplied context only — no evidence that anyone acted on it is recorded.",
        parameters: Type.Object({
            bundle_id: Type.String({ description: "Delivery reference from a receipt (the `bundle_id` / `receipt_ref`)." }),
            limit: Type.Optional(Type.Number({ description: "Maximum supplied records to show; the rest are counted." })),
        }),
        async execute(_id: string, params: any) {
            try {
                // `view` is not offered here: the caller's own query prose is
                // diagnostics-only, and reading it is an operator action on the
                // CLI, the REST API or the local inspector.
                const query = new URLSearchParams({ view: "public" });
                if (params.limit !== undefined) query.set("limit", String(params.limit));
                const res = await palinodeFetch(
                    cfg.palinodeApiUrl,
                    `/explain/${encodeURIComponent(params.bundle_id)}?${query.toString()}`,
                );
                return { content: [{ type: "text", text: JSON.stringify(res, null, 2) }] };
            } catch (err) {
                return { content: [{ type: "text", text: `Palinode explain failed: ${String(err)}` }] };
            }
        },
    }, { name: "palinode_explain" });

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

    api.registerTool({
        name: "palinode_corrections",
        label: "Palinode Corrections",
        description: "List correction candidates mined from harness session transcripts — moments a user overturned a decision, rejected an approach with a reason, or asked for something to be remembered. Advisory proposals, never applied; empty unless the store's operator enabled transcript capture.",
        parameters: Type.Object({
            project: Type.Optional(Type.String({ description: "Only candidates scoped to this project." })),
            since_days: Type.Optional(Type.Number({ description: "Only candidates from the last N days." })),
        }),
        async execute(_id: string, params: any) {
            try {
                // `scan` is not offered here: running a pass over someone's
                // transcripts is an operator action on the CLI or REST API.
                const body: Record<string, unknown> = { scan: false };
                if (params.project) body.project = params.project;
                if (params.since_days !== undefined) body.since_days = params.since_days;
                const res = await palinodeFetch(cfg.palinodeApiUrl, "/corrections", {
                    method: "POST",
                    body: JSON.stringify(body),
                });
                const candidates = res?.candidates || [];
                if (candidates.length === 0) {
                    const why = res?.enabled ? "" : " (transcript correction capture is disabled)";
                    return { content: [{ type: "text", text: `No correction candidates${why}.` }] };
                }
                const lines = candidates.map((c: any) =>
                    `${c.classification} · ${c.project || "unscoped"} · turn ${c.turn_index}: "${c.span}"`,
                );
                return { content: [{ type: "text", text: `${lines.join("\n")}\n\n${res.applied}` }] };
            } catch (err) {
                return { content: [{ type: "text", text: `Palinode corrections failed: ${String(err)}` }] };
            }
        },
    }, { name: "palinode_corrections" });

    // ========================================================================
    // Correction review — preview → apply / dismiss, plus undo.
    //
    // A delivery adapter over the same four routes the CLI and MCP call
    // (ADR-019). Preview reads; apply refuses without `confirm` and the
    // revision preview returned; undo previews unless confirmed. A 409 is the
    // contract working — a stale revision, an ambiguous target — so its
    // payload is handed back rather than flattened into a status line.
    // ========================================================================

    const correctionCall = async (phase: string, body: Record<string, unknown>, label: string) => {
        try {
            const res = await palinodeFetchWithStatus(cfg.palinodeApiUrl, `/corrections/${phase}`, {
                method: "POST",
                body: JSON.stringify(body),
            });
            if (res.ok) {
                return { content: [{ type: "text", text: JSON.stringify(res.body, null, 2) }] };
            }
            const detail = res.body?.detail;
            if (res.status === 409 && detail && typeof detail === "object") {
                // A partial is the opposite of a refusal: half of it landed.
                // Leading with "refused" would invite a re-run instead of the
                // one command that finishes the job.
                const lead = detail.applied === "partial"
                    ? `${label} PARTIALLY applied`
                    : `${label} refused`;
                return { content: [{ type: "text", text: `${lead}: ${JSON.stringify(detail, null, 2)}` }] };
            }
            return { content: [{ type: "text", text: `${label} failed: ${res.status} ${res.statusText}` }] };
        } catch (err) {
            return { content: [{ type: "text", text: `${label} failed: ${String(err)}` }] };
        }
    };

    const correctionBody = (params: any, keys: string[]) => {
        const body: Record<string, unknown> = {};
        for (const key of keys) {
            if (params[key] !== undefined && params[key] !== null) body[key] = params[key];
        }
        return body;
    };

    const CORRECTION_TARGET_PARAMS = {
        allow_content_loss: Type.Optional(Type.Boolean({ description: "Explicitly permit dropping the original text listed by preview." })),
        target: Type.Optional(Type.String({ description: "Memory to correct: 'decisions/x.md', 'decisions/x', or a bare slug." })),
        claim_id: Type.Optional(Type.String({ description: "Narrow the correction to one '<!-- fact:id -->' claim in the target." })),
        replacement: Type.Optional(Type.String({ description: "The text that stands instead. Omit to retire with no successor." })),
        action: Type.Optional(Type.Union([Type.Literal("supersede"), Type.Literal("retire")], { description: "supersede or retire; derived from replacement when omitted." })),
        reason: Type.Optional(Type.String({ description: "Why. Recorded in the history sibling and the commit subject." })),
        candidate_id: Type.Optional(Type.String({ description: "The correction candidate this came from." })),
        project: Type.Optional(Type.String({ description: "Project scope. Inferred from the target when omitted." })),
        backed_by: Type.Optional(Type.Array(Type.String(), { description: "Records supporting the REPLACEMENT. The superseded original is never cited as support for its replacement." })),
    };

    const CORRECTION_KEYS = ["target", "claim_id", "replacement", "action", "reason", "candidate_id", "project", "backed_by", "allow_content_loss"];

    api.registerTool({
        name: "palinode_correction_preview",
        label: "Palinode Correction Preview",
        description: "Show what correcting or retiring one memory would change, and change nothing. Returns the old text, the proposed new text, the target's exact revision, the rationale, the correction's source, the scope, the supersedes/superseded_by relation that would be recorded, the records that reference the target (reported, never rewritten) and the recovery command.",
        parameters: Type.Object({ ...CORRECTION_TARGET_PARAMS }),
        async execute(_id: string, params: any) {
            return correctionCall("preview", correctionBody(params, CORRECTION_KEYS), "Palinode correction preview");
        },
    }, { name: "palinode_correction_preview" });

    api.registerTool({
        name: "palinode_correction_apply",
        label: "Palinode Correction Apply",
        description: "Apply a correction that was previewed. Requires confirm=true and the expect_revision the preview returned; a target that changed since, or a ref matching more than one memory, is refused rather than guessed. The original is archived with superseded_by, never deleted. `applied: \"partial\"` means the replacement was saved and the original was NOT retired — run the complete_command it returns rather than re-running the correction.",
        parameters: Type.Object({
            ...CORRECTION_TARGET_PARAMS,
            expect_revision: Type.String({ description: "The revision palinode_correction_preview returned." }),
            confirm: Type.Boolean({ description: "Must be true. Nothing is written without it." }),
        }),
        async execute(_id: string, params: any) {
            if (!params.confirm) {
                return { content: [{ type: "text", text: "Palinode correction apply refused: confirm=true and the preview's expect_revision are both required." }] };
            }
            const body = correctionBody(params, [...CORRECTION_KEYS, "expect_revision"]);
            body.confirm = true;
            return correctionCall("apply", body, "Palinode correction apply");
        },
    }, { name: "palinode_correction_apply" });

    api.registerTool({
        name: "palinode_correction_dismiss",
        label: "Palinode Correction Dismiss",
        description: "Record that a correction candidate was reviewed and declined. Writes no memory: the queue row is marked dismissed with its reason and kept, which is what stops a later scan re-proposing the same span.",
        parameters: Type.Object({
            candidate_id: Type.String({ description: "The candidate id from palinode_corrections." }),
            reason: Type.String({ description: "Why it was declined. This is the record." }),
        }),
        async execute(_id: string, params: any) {
            if (!params.reason) {
                return { content: [{ type: "text", text: "Palinode correction dismiss refused: a reason is required — it is the record." }] };
            }
            return correctionCall("dismiss", correctionBody(params, ["candidate_id", "reason"]), "Palinode correction dismiss");
        },
    }, { name: "palinode_correction_dismiss" });

    api.registerTool({
        name: "palinode_correction_undo",
        label: "Palinode Correction Undo",
        description: "Preview (default) or apply the undo of a correction. Restoring a previous assertion, deleting history and undoing an agent's external actions are three different things: the result names all three, does the first, refuses the second and cannot reach the third.",
        parameters: Type.Object({
            target: Type.String({ description: "The archived memory to restore." }),
            expect_revision: Type.Optional(Type.String({ description: "The revision the undo preview returned. Required with confirm." })),
            confirm: Type.Optional(Type.Boolean({ description: "Must be true to write. Preview is the default." })),
            reason: Type.Optional(Type.String({ description: "Why the correction is being undone." })),
        }),
        async execute(_id: string, params: any) {
            return correctionCall("undo", correctionBody(params, ["target", "expect_revision", "confirm", "reason"]), "Palinode correction undo");
        },
    }, { name: "palinode_correction_undo" });

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

      api.on("before_prompt_build", async (event: any, context: any) => {
        if (!event.prompt || event.prompt.length < 3) return;
        const scope = automaticScope(event, context);
        const control = await automaticCheck(cfg.palinodeApiUrl, "recall", scope);
        if (!control.allowed) return;
        const project = control.project;

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

          // Phase 1: Discover core files through the server visibility policy.
          // Gated by `profile.sources.includes("core")` (#391/#394) — profiles
          // like "monitoring" and "investigation" skip core entirely.
          // Within core-enabled profiles, midTurnMode still controls full vs
          // summary vs none for mid-turns.
          const CORE_FILE_MAX = profile.coreMaxCharsPerFile ?? 3000;
          const CORE_TOTAL_MAX = profile.coreBudget ?? 8000;
          const midTurnMode = cfg.midTurnMode || "none";
          let coreBudgetRemaining = CORE_TOTAL_MAX;

          const coreEnabled = profile.sources.includes("core");
          // On non-full turns with mode "none", skip core entirely.
          // Profile-disabled core also skips entirely.
          if (!coreEnabled || (!fullCoreThisTurn && midTurnMode === "none")) {
            // No core injection — the model still has turn 1's context (or core is profile-disabled)
          } else {
            try {
              // /list has no caller scope; it withholds private/restricted and
              // expired cores. Only paths selected there become automatic reads.
              const signal = AbortSignal.timeout(5000);
              const files = await palinodeFetch(cfg.palinodeApiUrl, "/list?core_only=true", { signal });
              if (!Array.isArray(files)) throw new Error("Invalid core selection response");
              for (const file of files) {
                if (coreBudgetRemaining <= 0) break;
                if (typeof file?.file !== "string" || file.core !== true || !belongsToProject(file, project)) continue;
                const summary = typeof file.summary === "string" ? file.summary.trim() : "";
                const source = `\n--- ${file.file} ---\n`;
                let block: string;
                if (!fullCoreThisTurn && midTurnMode === "summary") {
                  if (!summary) continue;
                  block = `${source}> ${summary}\n`;
                } else {
                  const result = await palinodeFetch(
                    cfg.palinodeApiUrl,
                    `/read?file_path=${encodeURIComponent(file.file)}&project=${encodeURIComponent(project ?? "")}&automatic=true`,
                    { signal },
                  );
                  if (typeof result?.content !== "string") throw new Error("Invalid core read response");
                  const content = result.content;
                  const maxForThis = Math.max(0, Math.min(CORE_FILE_MAX, coreBudgetRemaining));
                  let injected = content.slice(0, maxForThis);
                  if (content.length > maxForThis) {
                    injected += summary
                      ? `\n...[truncated — summary: ${summary}]`
                      : `\n...[truncated — full file at ${file.file}]`;
                  }
                  const header = summary ? `> ${summary}\n\n` : "";
                  block = `${source}${header}${injected}\n`;
                }
                block = block.slice(0, coreBudgetRemaining);
                coreContent += block;
                coreBudgetRemaining -= block.length;
              }
            } catch {
              api.logger.warn("openclaw-palinode: core recall unavailable or incomplete; continuing with available recall");
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
              if (project) semBody.context = [`project/${project}`];
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
              let assoc = await palinodeFetch(cfg.palinodeApiUrl, "/search-associative", {
                  method: "POST",
                  body: JSON.stringify(assocBody),
              });
              if (project && Array.isArray(assoc) && assoc.length > 0) {
                const files = await palinodeFetch(cfg.palinodeApiUrl, "/list", {
                  signal: AbortSignal.timeout(5000),
                });
                if (!Array.isArray(files)) throw new Error("Invalid associative selection response");
                const visiblePaths = new Set(files.filter((file: any) => belongsToProject(file, project))
                  .map((file: any) => file.file));
                assoc = assoc.filter((r: any) => {
                  const relativePath = r.rel_path ?? r.file_path;
                  if (typeof relativePath !== "string") return false;
                  const file = path.isAbsolute(relativePath)
                    ? path.relative(cfg.palinodeDir, relativePath) : relativePath;
                  return visiblePaths.has(file);
                });
              }
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
                  // Older trigger APIs do not enforce visibility. Select before
                  // including even its description or path in automatic context.
                  const signal = AbortSignal.timeout(5000);
                  const files = await palinodeFetch(cfg.palinodeApiUrl, "/list", { signal });
                  if (!Array.isArray(files)) throw new Error("Invalid trigger selection response");
                  const visiblePaths = new Set(files.filter((file: any) => belongsToProject(file, project))
                    .map((file: any) => file.file));
                  const selected = triggers.filter((t: any) =>
                    typeof t.memory_file === "string" && visiblePaths.has(t.memory_file),
                  ).slice(0, nMax);
                  for (const t of selected) {
                    const result = await palinodeFetch(
                      cfg.palinodeApiUrl,
                      `/read?file_path=${encodeURIComponent(t.memory_file)}&project=${encodeURIComponent(project ?? "")}&automatic=true`,
                      { signal },
                    );
                    if (typeof result?.content !== "string") throw new Error("Invalid trigger read response");
                    triggerContent += `\n\n--- Triggered: ${t.description} (${t.memory_file}) ---\n${result.content.slice(0, triggerCap)}`;
                  }
              }
            } catch {
              api.logger.warn("openclaw-palinode: trigger recall unavailable or incomplete; continuing with available recall");
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

          if (!await automaticAllowed(cfg.palinodeApiUrl, "recall", scope)) return;
          return { systemContext: injection };
        } catch (err) {
          api.logger.warn(`openclaw-palinode: recall failed: ${String(err)}`);
        }
      });
    }

    // Capture uses the API policy and git provenance boundary.
    const captureSession = async (event: any, context: any, trigger: string) => {
      if (!cfg.autoCapture || !event.messages?.length) return;
      const scope = automaticScope(event, context);
      try {
        if (!await automaticAllowed(cfg.palinodeApiUrl, "capture", scope)) return;
        const messages: string[] = [];
        for (const message of event.messages.slice(-20)) {
          if (message?.role !== "user" && message?.role !== "assistant") continue;
          const content = typeof message.content === "string" ? message.content
            : Array.isArray(message.content) ? message.content
              .filter((part: any) => typeof part?.text === "string")
              .map((part: any) => part.text).join("\n") : "";
          const text = content.replace(/<palinode-memory>[\s\S]*?<\/palinode-memory>\s*/g, "").trim();
          if (text) messages.push(`${message.role}: ${text.slice(0, 500)}`);
        }
        if (!messages.length) return;
        await palinodeFetch(cfg.palinodeApiUrl, "/session-end", {
          method: "POST", signal: AbortSignal.timeout(10000),
          body: JSON.stringify({
            summary: `Auto-captured (openclaw ${trigger}).\n${messages.join("\n\n").slice(0, 2000)}`,
            source: "openclaw-plugin", automatic: true, ...scope,
            decisions: [], blockers: [],
          }),
        });
        api.logger.info("openclaw-palinode: bounded session capture accepted by API");
      } catch {
        api.logger.warn("openclaw-palinode: automatic capture unavailable");
      }
    };

    if (cfg.autoCapture) {
      api.on("agent_end", async (event: any, context: any) => {
        if (event.success) await captureSession(event, context, "agent_end");
      });
      api.on("before_reset", async (event: any, context: any) => {
        await captureSession(event, context, "before_reset");
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
              const project = await resolveClientProject(cfg.palinodeApiUrl, { cwd: process.cwd() });
              const results = await palinodeFetch(cfg.palinodeApiUrl, "/search", {
                method: "POST",
                body: JSON.stringify({
                  ...(project ? { context: [`project/${project}`] } : {}),
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
