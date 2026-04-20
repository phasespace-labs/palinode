/**
 * OpenClaw Palinode Plugin
 *
 * Persistent memory system — files as truth, vectors as search.
 * Connects to the Palinode Python API for search/save operations.
 * Injects core memory at session start, extracts memories at session end.
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
};

type PalinodeConfig = {
  palinodeApiUrl: string;
  palinodeDir: string;
  promptsDir: string;
  autoCapture: boolean;
  autoRecall: boolean;
  midTurnMode: "none" | "summary" | "full";
};

// Config schema follows standard OpenClaw plugin pattern
const _palinodeConfigSchema = Type.Object({
  palinodeApiUrl: Type.String({ default: DEFAULTS.palinodeApiUrl }),
  palinodeDir: Type.String({ default: DEFAULTS.palinodeDir }),
  promptsDir: Type.String({ default: DEFAULTS.promptsDir }),
  autoCapture: Type.Boolean({ default: DEFAULTS.autoCapture }),
  autoRecall: Type.Boolean({ default: DEFAULTS.autoRecall }),
});

const palinodeConfigSchema = {
  ..._palinodeConfigSchema,
  parse(value: unknown): PalinodeConfig {
    const cfg = (value || {}) as Record<string, unknown>;
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
    };
  },
};

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
        }),
        async execute(_toolCallId: string, params: any) {
          try {
            const results = await palinodeFetch(
              cfg.palinodeApiUrl,
              "/search",
              {
                method: "POST",
                body: JSON.stringify({
                  query: params.query,
                  category: params.category,
                  limit: params.limit || 5,
                }),
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

        // Skip semantic search for trivial/short messages — just inject core
        const trivialMessage = event.prompt.trim().length < 15 ||
          /^(ok|yep|yay|hmm|hm|yes|no|sure|thanks|thx|cool|got it|nice|lol|k|👍|👎|✅|🙏)\.?$/i.test(event.prompt.trim());

        try {
          let coreContent = "";

          // Phase 1: Load all files with core: true
          // Turn 1 and every N turns: full content
          // Mid-turns: controlled by midTurnMode config:
          //   "none"    — skip core entirely (just topic search, saves ~200 tokens/turn)
          //   "summary" — inject one-line summaries only
          //   "full"    — inject full core every turn (expensive)
          const CORE_FILE_MAX = 3000;  // Per-file character limit
          const CORE_TOTAL_MAX = 8000; // Total budget for all core files (~2K tokens)
          const midTurnMode = cfg.midTurnMode || "none";
          const dirsToScan = ["people", "projects", "decisions", "insights"];
          let coreBudgetRemaining = CORE_TOTAL_MAX;

          // On non-full turns with mode "none", skip core entirely
          if (!fullCoreThisTurn && midTurnMode === "none") {
            // No core injection — the model still has turn 1's context
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

          // Phase 2: Topic-specific retrieval based on the user's message
          // Skip for trivial messages — no value in searching for "ok" or "yay"
          let topicContent = "";
          if (!trivialMessage) {
            try {
              const results = await palinodeFetch(cfg.palinodeApiUrl, "/search", {
                method: "POST",
                body: JSON.stringify({
                  query: event.prompt,
                  limit: 5,
                }),
              });
              if (results && results.length > 0) {
                topicContent = results
                  .map(
                    (r: any) =>
                      `[${r.category || "memory"}] ${r.content.slice(0, 700)}`,
                  )
                  .join("\n\n");
              }
            } catch {
              // Search unavailable — degrade gracefully
            }
          }

          if (!coreContent && !topicContent) return;

          // Phase 3: Associative Recall (Entity Graph)
          let assocContent = "";
          if (!trivialMessage) {
            try {
              const assoc = await palinodeFetch(cfg.palinodeApiUrl, "/search-associative", {
                  method: "POST",
                  body: JSON.stringify({ query: event.prompt, seed_entities: [], limit: 3 })
              });
              if (assoc && assoc.length > 0) {
                  assocContent = assoc.map(
                      (r: any) => `[Related Context via Entity Graph] File: ${r.file_path}\n${r.content.slice(0, 500)}`
                  ).join("\n\n");
              }
            } catch {
              // degrade gracefully
            }
          }

          // Phase 4: Prospective Triggers
          let triggerContent = "";
          if (!trivialMessage) {
            try {
              const triggers = await palinodeFetch(cfg.palinodeApiUrl, "/check-triggers", {
                  method: "POST",
                  body: JSON.stringify({ query: event.prompt })
              });
              if (triggers && triggers.length > 0) {
                  triggerContent = triggers.map((t: any) => {
                      const triggerPath = resolveWithin(cfg.palinodeDir, t.memory_file);
                      if (!triggerPath) {
                          return `\n[TRIGGER SKIPPED: ${t.description}] -> Unsafe path: ${t.memory_file}`;
                      }
                      const content = readFileIfExists(triggerPath);
                      if (content) {
                          return `\n\n--- Triggered: ${t.description} (${t.memory_file}) ---\n${content.slice(0, 2000)}`;
                      } else {
                          return `\n[TRIGGER FIRED: ${t.description}] -> File: ${t.memory_file} (file not found)`;
                      }
                  }).join("\n\n");
              }
            } catch {
              // degrade gracefully
            }
          }

          let injection = "<palinode-memory>\n";
          if (coreContent) {
            // Capacity display line — inspired by NousResearch/hermes-agent prompt_builder.py
            // Lets the agent see how full core memory is and consolidate proactively
            const totalChars = coreContent.length;
            const maxChars = CORE_TOTAL_MAX;
            const pct = Math.round((totalChars / maxChars) * 100);
            const capacityLine = `[Core Memory: ${totalChars.toLocaleString()} / ${maxChars.toLocaleString()} chars — ${pct}%]\n\n`;
            injection += `## Core Memory\n${capacityLine}${coreContent}\n`;
          }
          if (topicContent) {
            injection += `\n## Relevant Context\n${topicContent}\n`;
          }
          if (assocContent) {
            injection += `\n## Associative Context\n${assocContent}\n`;
          }
          if (triggerContent) {
            injection += `\n## IMPORTANT Triggers\n${triggerContent}\n`;
          }
          injection += "</palinode-memory>";

          // Scrub sensitive content before injection
          injection = scrubSensitive(injection, cfg.palinodeDir);

          api.logger.info(
            `openclaw-palinode: turn ${sessionTurnCount} — ${fullCoreThisTurn ? "FULL core" : "summary-only"} + ${topicContent ? "topic search" : "no search"}`,
          );

          if (pendingEsReceipt) {
            injection = pendingEsReceipt + "\n\n" + injection;
            pendingEsReceipt = null;
          }

          return { prependContext: injection };
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
