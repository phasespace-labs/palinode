/**
 * palinode-plugin-core — the shared TypeScript core for Palinode's harness
 * plugins (ADR-019 delivery adapters; extracted on the third plugin per #1002).
 *
 * Everything here is a plain function over an injected `fetch`, so the whole
 * recall/prime/capture surface is testable without any harness installed.
 * Each harness binding (`plugins/pi`, `plugins/cline`) is deliberately thin:
 * it wires these functions to lifecycle events and nothing else.
 *
 * What lives here is exactly what the plugins duplicated before extraction —
 * nothing hook-shaped, nothing harness-shaped:
 *   - the fail-open REST client (bearer, timeout, HTTP>=400 → null)
 *   - config resolution from the shared env knobs (+ recall profiles)
 *   - recall → injection TEXT (triggers + strict search, bounded)
 *   - session-start priming digest
 *   - the capture-floor payload for /session-end
 *   - the reversal client (restore / unretract / forget-withdraw), so a
 *     binding that exposes archival can expose its undo on the same path
 *
 * Design contract (shared with the Claude Code hooks — same knobs, same
 * semantics, same env var names):
 *   - Fail-open everywhere. API down, timeout, bad JSON → null, never throw.
 *   - Silence is the common case and must be free: no recall → no message.
 *   - Injected recall is bounded: few results, tight snippets, total cap.
 *
 * THE ONE INVARIANT THIS CORE OWNS (ADR-019 §4, #1002): everything this
 * module produces for injection is a *message body*. There is no function
 * here that yields a system prompt, and no binding may route these strings
 * into one. Model providers cache the prompt as a strict prefix
 * (tools → system → messages); per-turn content in the system prompt
 * invalidates that whole cached prefix every turn and costs more than the
 * recall saves. Bindings append a message after the cached prefix instead,
 * and each binding's test suite pins that.
 */

export interface PalinodeConfig {
  apiUrl: string;
  token?: string;
  /** Named recall profile the channel knobs were derived from. */
  recallProfile: RecallProfileName;
  /** Search hits injected per prompt; 0 disables the search channel. */
  maxResults: number;
  /** Similarity floor for per-turn search. */
  threshold: number;
  /** Trigger channel on/off. */
  triggersOn: boolean;
  /** Prompts shorter than this skip recall entirely. */
  minChars: number;
  /** Total cap on per-turn injected context. */
  maxChars: number;
  /** Per-request timeout in milliseconds. */
  timeoutMs: number;
  /** Max core memories in the session-start digest; 0 disables priming. */
  coreMaxFiles: number;
  /** Total cap on the session-start digest. */
  coreMaxChars: number;
  /** Minimum user messages before session capture fires. */
  minMessages: number;
}

/**
 * Recall profiles — the same vocabulary the OpenClaw plugin uses, expressed
 * in the hook-shaped knobs above: which channels are on and how much they may
 * inject. A profile is a starting point; explicit env knobs win over it.
 */
export type RecallProfileName =
  | "coding"
  | "monitoring"
  | "investigation"
  | "writing"
  | "conversation"
  | "minimal"
  | "off";

type ProfileKnobs = Pick<PalinodeConfig, "maxResults" | "triggersOn" | "coreMaxFiles">;

export const PROFILES: Record<RecallProfileName, ProfileKnobs> = {
  /** Everything on: priming, triggers, strict search. The default. */
  coding: { maxResults: 3, triggersOn: true, coreMaxFiles: 10 },
  /** Prospective triggers only — for cron/monitor prompts. */
  monitoring: { maxResults: 0, triggersOn: true, coreMaxFiles: 0 },
  /** Search only, wider net — diagnosis sessions. */
  investigation: { maxResults: 8, triggersOn: false, coreMaxFiles: 0 },
  /** Priming only — standing context, no per-turn recall. */
  writing: { maxResults: 0, triggersOn: false, coreMaxFiles: 10 },
  /** Priming + triggers, no search. */
  conversation: { maxResults: 0, triggersOn: true, coreMaxFiles: 10 },
  minimal: { maxResults: 0, triggersOn: false, coreMaxFiles: 0 },
  off: { maxResults: 0, triggersOn: false, coreMaxFiles: 0 },
};

export function isRecallProfileName(value: unknown): value is RecallProfileName {
  return typeof value === "string" && value in PROFILES;
}

/** Per-fired-trigger content cap and max fired triggers injected per prompt.
 *  Mirrors the Claude Code hook's constants. */
const TRIGGER_READ_CHARS = 1200;
const TRIGGER_MAX_FIRED = 2;
/** Per-result snippet cap requested from /search. */
const SNIPPET_MAX_CHARS = 300;

type Env = Record<string, string | undefined>;

function num(env: Env, key: string, fallback: number): number {
  const raw = env[key];
  if (raw === undefined || raw === "") return fallback;
  const n = Number(raw);
  return Number.isFinite(n) ? n : fallback;
}

/** Same env vars as the Claude Code hooks — one set of knobs, every harness.
 *  `overrides` (from a harness's own config surface) win over the env. */
export function configFromEnv(
  env: Env = process.env,
  overrides: Partial<PalinodeConfig> = {},
): PalinodeConfig {
  const profileName = isRecallProfileName(overrides.recallProfile)
    ? overrides.recallProfile
    : isRecallProfileName(env.PALINODE_HOOK_RECALL_PROFILE)
      ? env.PALINODE_HOOK_RECALL_PROFILE
      : "coding";
  const profile = PROFILES[profileName];
  const fromEnv: PalinodeConfig = {
    apiUrl: env.PALINODE_API_URL ?? "http://localhost:6340",
    token: env.PALINODE_API_TOKEN || undefined,
    recallProfile: profileName,
    maxResults: num(env, "PALINODE_HOOK_RECALL_MAX_RESULTS", profile.maxResults),
    // Raw-cosine floor, calibrated in the server's SearchConfig against real
    // bge-m3 (54 pairs): true matches clear 0.5 at 98% but 0.7 at only 28%.
    // An earlier 0.75 default made the search channel silently dead.
    threshold: num(env, "PALINODE_HOOK_RECALL_THRESHOLD", 0.5),
    triggersOn:
      env.PALINODE_HOOK_RECALL_TRIGGERS === undefined || env.PALINODE_HOOK_RECALL_TRIGGERS === ""
        ? profile.triggersOn
        : env.PALINODE_HOOK_RECALL_TRIGGERS !== "0",
    minChars: num(env, "PALINODE_HOOK_RECALL_MIN_CHARS", 12),
    maxChars: num(env, "PALINODE_HOOK_RECALL_MAX_CHARS", 3000),
    timeoutMs: num(env, "PALINODE_HOOK_RECALL_TIMEOUT", 4) * 1000,
    coreMaxFiles: num(env, "PALINODE_HOOK_INJECT_MAX_FILES", profile.coreMaxFiles),
    coreMaxChars: num(env, "PALINODE_HOOK_INJECT_MAX_CHARS", 4000),
    minMessages: num(env, "PALINODE_HOOK_MIN_MESSAGES", 3),
  };
  const defined = Object.fromEntries(
    Object.entries(overrides).filter(([, v]) => v !== undefined),
  ) as Partial<PalinodeConfig>;
  return { ...fromEnv, ...defined, recallProfile: profileName };
}

export type FetchFn = typeof fetch;

/** One fail-open request. Any failure — network, HTTP >= 400, bad JSON —
 *  resolves to null. The caller decides what silence means. */
export async function apiJson(
  cfg: PalinodeConfig,
  fetchFn: FetchFn,
  path: string,
  init?: { method?: string; body?: unknown },
): Promise<unknown | null> {
  try {
    const headers: Record<string, string> = {};
    if (init?.body !== undefined) headers["Content-Type"] = "application/json";
    if (cfg.token) headers["Authorization"] = `Bearer ${cfg.token}`;
    const res = await fetchFn(`${cfg.apiUrl}${path}`, {
      method: init?.method ?? (init?.body !== undefined ? "POST" : "GET"),
      headers,
      body: init?.body !== undefined ? JSON.stringify(init.body) : undefined,
      signal: AbortSignal.timeout(cfg.timeoutMs),
    });
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  }
}

interface SearchHit {
  rel_path?: string;
  file_path?: string;
  /** Post-fusion rank value — ~1.0 for any query's top hit. Display fallback only. */
  score?: number;
  /** Raw cosine — the scale the threshold knob filters on. Preferred for display. */
  raw_score?: number | null;
  snippet?: string;
  content?: string;
}

/** Keep aligned with palinode/core/scoring.py; the packages remain independently deployable. */
function describeMatch(result: SearchHit): string {
  const rank = (result.score ?? 0).toFixed(2);
  if (result.raw_score === null) return `keyword match, rank ${rank}`;
  if (typeof result.raw_score === "number") {
    return `${Math.round(result.raw_score * 100)}% match`;
  }
  return `rank ${rank}`;
}

interface FiredTrigger {
  memory_file?: string;
}

/**
 * Per-turn recall: prospective triggers + strict-threshold search.
 * Returns the context block to inject, or null when there is nothing to say.
 */
export async function buildRecallContext(
  prompt: string,
  cfg: PalinodeConfig,
  fetchFn: FetchFn = fetch,
): Promise<string | null> {
  if (prompt.length < cfg.minChars) return null;

  // The two channels are independent; run them concurrently (the hosts'
  // hook budgets are tight — Cline's sandbox allows 3 s for the whole hook).
  // Output order stays fixed: triggers first, then search.
  const triggerSection = async (): Promise<string> => {
    if (!cfg.triggersOn) return "";
    const fired = await apiJson(cfg, fetchFn, "/check-triggers", {
      body: { query: prompt },
    });
    if (!Array.isArray(fired)) return "";
    let out = "";
    for (const t of (fired as FiredTrigger[]).slice(0, TRIGGER_MAX_FIRED)) {
      if (!t.memory_file) continue;
      const read = (await apiJson(
        cfg,
        fetchFn,
        `/read?file_path=${encodeURIComponent(t.memory_file)}`,
      )) as { content?: string } | null;
      const body = read?.content;
      if (body) {
        out += `\n### Trigger fired: ${t.memory_file}\n${body.slice(0, TRIGGER_READ_CHARS)}\n`;
      }
    }
    return out;
  };

  const searchSection = async (): Promise<string> => {
    if (cfg.maxResults <= 0) return "";
    const hits = (await apiJson(cfg, fetchFn, "/search", {
      body: {
        query: prompt,
        limit: cfg.maxResults,
        threshold: cfg.threshold,
        max_chars: SNIPPET_MAX_CHARS,
      },
    })) as { results?: SearchHit[] } | SearchHit[] | null;
    const results = Array.isArray(hits) ? hits : hits?.results;
    if (!Array.isArray(results) || results.length === 0) return "";
    const lines = results
      .map((r) => {
        const path = r.rel_path ?? r.file_path ?? "?";
        const body = (r.snippet ?? r.content ?? "").replace(/\n/g, " ");
        return `- [${path}] (${describeMatch(r)}) ${body}`;
      })
      .join("\n");
    return `\n### Related memories\n${lines}\n`;
  };

  const sections = (await Promise.all([triggerSection(), searchSection()])).join("");
  if (!sections) return null;

  const context = `## Palinode recall (this prompt)

Retrieved from persistent memory; may be stale — verify before relying on
it. More detail: palinode_search / palinode_read.
${sections}`;

  return context.slice(0, cfg.maxChars);
}

interface CoreListEntry {
  file?: string;
  name?: string;
  summary?: string;
}

/**
 * Session-start priming: warm server-side session context, then return a
 * bounded digest of `core: true` memories — or null when there are none.
 */
export async function buildCoreDigest(
  cfg: PalinodeConfig,
  fetchFn: FetchFn = fetch,
  cwd: string = process.cwd(),
  sessionId = "",
): Promise<string | null> {
  // Warm /context/prime; result deliberately ignored (older servers 404).
  await apiJson(cfg, fetchFn, "/context/prime", {
    body: { cwd, session_id: sessionId },
  });

  if (cfg.coreMaxFiles <= 0) return null;

  const listing = await apiJson(cfg, fetchFn, "/list?core_only=true");
  if (!Array.isArray(listing) || listing.length === 0) return null;

  const lines = (listing as CoreListEntry[])
    .slice(0, cfg.coreMaxFiles)
    .map((e) => {
      const name = e.name ?? "untitled";
      const summary = e.summary ? ` — ${e.summary}` : "";
      return `- [${e.file ?? "?"}] ${name}${summary}`;
    })
    .join("\n");

  const context = `## Palinode memory (session start)

Persistent memory is connected. Recall details with the palinode_search /
palinode_read tools — they read the live store; session notes are NOT
files in this repo.

Core memories:
${lines}`;

  return context.slice(0, cfg.coreMaxChars);
}

/**
 * The subset of a harness message the capture floor reads. Covers Pi session
 * entries (`{ type, message: { role, content } }`) and Cline agent messages
 * (`{ role, content: [{ type: "text", text }] }`) without importing either.
 */
export interface SessionEntryLike {
  type?: string;
  role?: string;
  content?: unknown;
  message?: { role?: string; content?: unknown };
}

function entryRole(e: SessionEntryLike): string | undefined {
  return e.role ?? e.message?.role ?? e.type;
}

function contentText(c: unknown): string {
  if (typeof c === "string") return c;
  if (Array.isArray(c)) {
    return c
      .map((b) => (typeof b === "object" && b && "text" in b ? String((b as { text: unknown }).text) : ""))
      .join(" ");
  }
  return "";
}

/** Text of an entry's user-facing content, or "" when it has none. */
export function entryText(e: SessionEntryLike): string {
  return contentText(e.message?.content ?? e.content);
}

/** Entries a human typed — what the capture floor counts. */
export function userEntries(entries: SessionEntryLike[]): SessionEntryLike[] {
  return entries.filter((e) => entryRole(e) === "user" && entryText(e).trim() !== "");
}

export interface CaptureOrigin {
  project: string;
  /** Recorded as the memory's `source` (e.g. "pi-extension", "cline-plugin"). */
  source: string;
  /** Harness label for the /session-end metadata footer. */
  harness: string;
  /** Which lifecycle event fired the capture (e.g. "session_shutdown", "run_end"). */
  trigger: string;
  sessionId?: string;
  cwd?: string;
}

export interface SessionCapturePayload {
  summary: string;
  project: string;
  source: string;
  harness: string;
  trigger: string;
  decisions: string[];
  blockers: string[];
  session_id?: string;
  cwd?: string;
}

/**
 * Session capture floor: derive the minimal /session-end payload from the
 * session entries. Returns null when the session is too trivial to keep
 * (fewer than `minMessages` user messages) — same gate as the Claude Code
 * floor hook.
 */
export function buildSessionCapture(
  entries: SessionEntryLike[],
  cfg: PalinodeConfig,
  origin: CaptureOrigin,
): SessionCapturePayload | null {
  const users = userEntries(entries);
  if (users.length < cfg.minMessages) return null;

  const squash = (s: string) => s.replace(/\s+/g, " ").trim().slice(0, 200);
  const firstPrompt = squash(entryText(users[0]));
  const lastPrompt = squash(entryText(users[users.length - 1]));
  const latest = users.length > 1 && lastPrompt !== firstPrompt ? ` Latest: ${lastPrompt}` : "";

  return {
    summary: `Auto-captured (${origin.harness} ${origin.trigger}, ${users.length} messages). Topic: ${firstPrompt}${latest}`,
    project: origin.project,
    source: origin.source,
    harness: origin.harness,
    trigger: origin.trigger,
    decisions: [],
    blockers: [],
    ...(origin.sessionId ? { session_id: origin.sessionId } : {}),
    ...(origin.cwd ? { cwd: origin.cwd } : {}),
  };
}

/** POST the capture; fail-open. Returns true when the API accepted it. */
export async function postSessionCapture(
  payload: SessionCapturePayload,
  cfg: PalinodeConfig,
  fetchFn: FetchFn = fetch,
): Promise<boolean> {
  const res = await apiJson(cfg, fetchFn, "/session-end", { body: payload });
  return res !== null;
}

// ---------------------------------------------------------------------------
// Reversal client — the inverse of archival and retraction, over the REST
// API. Plain fail-open calls: the result object on 2xx, null otherwise. Each
// maps 1:1 to the registered operation in palinode/core/parity.py so a
// binding exposing them inherits the canonical parameter names.
// ---------------------------------------------------------------------------

export interface RestoreResult {
  file: string;
  /** "active" when restored; "not_archived" when there was nothing to undo. */
  status: string;
  restored_from?: string | null;
  restored_at?: string;
  history_file?: string | null;
  chunks_updated?: number;
  /** Present when the memory still carries a TTL the sweep will act on. */
  expires_at?: string;
}

/** Bring one archived memory back into default recall (inverse of /archive). */
export async function restoreMemory(
  filePath: string,
  cfg: PalinodeConfig,
  fetchFn: FetchFn = fetch,
  reason?: string,
): Promise<RestoreResult | null> {
  const body: Record<string, unknown> = { file_path: filePath };
  if (reason !== undefined) body.reason = reason;
  const res = await apiJson(cfg, fetchFn, "/restore", { body });
  return (res as RestoreResult | null) ?? null;
}

export interface UnretractResult {
  file: string;
  /** "unretracted", or "not_retracted" when the pref was not on record. */
  status: string;
  mentions: number;
  retraction_id?: string;
  history_file?: string | null;
  index_error?: string;
}

/** Withdraw one pref's mention-level retraction from one memory. */
export async function unretractMentions(
  filePath: string,
  pref: string,
  cfg: PalinodeConfig,
  fetchFn: FetchFn = fetch,
  reason?: string,
): Promise<UnretractResult | null> {
  const body: Record<string, unknown> = { file_path: filePath, pref };
  if (reason !== undefined) body.reason = reason;
  const res = await apiJson(cfg, fetchFn, "/unretract", { body });
  return (res as UnretractResult | null) ?? null;
}

export interface ForgetWithdrawResult {
  file: string;
  status: string;
  pref: string;
  restored: string[];
  unretracted: Array<{ path: string; mentions: number }>;
  requests_archived: string[];
  failed?: Array<{ path: string; op: string }>;
}

/** Take a forget request back: restore + unretract its targets, archive the record. */
export async function withdrawForgetRequest(
  filePath: string,
  cfg: PalinodeConfig,
  fetchFn: FetchFn = fetch,
  reason?: string,
): Promise<ForgetWithdrawResult | null> {
  const body: Record<string, unknown> = { file_path: filePath };
  if (reason !== undefined) body.reason = reason;
  const res = await apiJson(cfg, fetchFn, "/forget-withdraw", { body });
  return (res as ForgetWithdrawResult | null) ?? null;
}
