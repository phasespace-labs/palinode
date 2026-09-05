"""
Palinode Configuration

Loads settings from palinode.config.yaml with sensible defaults.
Environment variables override YAML values where noted.

Config resolution order:
  1. palinode.config.yaml in PALINODE_DIR (if exists)
  2. palinode.config.yaml in repo root (if exists)
  3. Built-in defaults (this file)
  4. Environment variable overrides (PALINODE_DIR, OLLAMA_URL, etc.)
"""
from __future__ import annotations

import logging
import os
import sys
import glob
from pathlib import Path
from dataclasses import field
from typing import Literal
from pydantic.dataclasses import dataclass
from pydantic import TypeAdapter, ValidationError
import yaml

_logger = logging.getLogger("palinode.config")
ToolSurface = Literal["core", "full"]
VALID_TOOL_SURFACES: set[str] = {"core", "full"}


def validate_tool_surface(value: str, source: str = "tool_surface") -> ToolSurface:
    normalized = value.strip().lower()
    if normalized not in VALID_TOOL_SURFACES:
        raise ValueError(
            f"{source} must be one of {sorted(VALID_TOOL_SURFACES)}, got {value!r}"
        )
    if normalized == "core":
        return "core"
    return "full"


def _expand_path(path_str: str) -> str:
    """Expand ~ and normalizes path."""
    return os.path.expanduser(path_str)

@dataclass
class CrossRefsConfig:
    """the mechanical cross-linking work: mechanical, untyped cross-linking during indexing.

    When enabled, the watcher scans an indexed memory's body for mentions of
    other memory files and records them in a ``cross_refs`` frontmatter list.
    ``min_token_len`` is the floor below which a bare slug/title token is NOT
    matched in prose, to avoid false-positive substring hits on short names.
    """
    enabled: bool = True
    min_token_len: int = 6

@dataclass
class CaptureConfig:
    """General capture capability configuration map.

    Only ``cross_refs`` is live — session extraction, daily-note capture, and
    quick-capture are all handled by their respective callers with no config
    read-through, so no dataclasses exist here for them.
    """
    cross_refs: CrossRefsConfig = field(default_factory=CrossRefsConfig)

@dataclass
class TranscriptorConfig:
    """Media transcription proxy service configuration."""
    url: str = "http://localhost:8787"
    timeout_seconds: int = 600

@dataclass
class IngestionConfig:
    """Document queue processing paths limitation controls."""
    inbox_dir: str = "inbox/raw"
    processed_dir: str = "inbox/processed"
    pdf_max_chars: int = 10000
    url_max_chars: int = 10000
    transcriptor: TranscriptorConfig = field(default_factory=TranscriptorConfig)

@dataclass
class PrimaryEmbeddingConfig:
    """Configuration for local embedding endpoints."""
    model: str = "bge-m3"
    url: str = "http://localhost:11434"
    dimensions: int = 1024
    timeout_seconds: int = 120
    connect_timeout_seconds: int = 10

@dataclass
class EmbeddingsConfig:
    """Embedding backend configuration."""
    primary: PrimaryEmbeddingConfig = field(default_factory=PrimaryEmbeddingConfig)

@dataclass
class AutoSummaryConfig:
    """Inference automation definitions for semantic summarization."""
    enabled: bool = True
    model: str = "qwen2.5:14b-instruct"
    max_chars: int = 120
    min_content_chars: int = 200
    ollama_url: str | None = None
    # wire protocol for the CHAT *primary* (model @ ollama_url). "ollama" =
    # Ollama-native /api/generate (the default; back-compat). "openai" = an
    # OpenAI-compatible /v1/chat/completions endpoint (LM Studio, vLLM, the
    # Sonnet shim, etc.) — set this when the primary is e.g. an MLX model served
    # by LM Studio. The llm_fallbacks chain below is always OpenAI-compat
    # regardless of this setting. When "openai", any primary failure (not just a
    # brownout) cascades to the fallback chain, since an OpenAI primary is
    # typically a remote host with configured backups.
    api: str = "ollama"
    # hard timeout for the /api/generate call inside _generate_description.
    # Default 5s so a cold Ollama model (15+ s latency) doesn't block /save.
    # Override via PALINODE_DESCRIBE_TIMEOUT_SECONDS env var.
    describe_timeout_seconds: float = 5.0
    # OpenAI-compat fallback chain for the CHAT role (auto-description /
    # auto-summary), walked in order on primary failure. Mirrors
    # ConsolidationConfig.llm_fallbacks — each entry is {model, url} pointing at
    # an OpenAI-compatible /v1/chat/completions endpoint (a second qwen host, the
    # Sonnet shim, etc.). With api="ollama" the chain fires when the native
    # primary browns out (OllamaTimeout / OllamaCircuitOpen); with api="openai"
    # it fires on any primary failure. Empty default = today's behavior, zero
    # change. Reached only from the watcher-driven /generate-summaries backfill
    # (the /save hot path doesn't enrich inline post-), so configured
    # fallbacks never egress on the save path.
    llm_fallbacks: list[dict] = field(default_factory=list)
    # per-/generate-summaries-run cap on files that may escalate to a CHAT
    # fallback. Bounds Anthropic egress when the local chat host is chronically
    # down and one backfill walk spans a large deferred backlog. 0 = unlimited
    # (explicit opt-out). Only applies when llm_fallbacks is non-empty.
    llm_fallback_max_per_run: int = 10

@dataclass
class SearchConfig:
    """Matching index score cutoffs thresholds layouts.

    mcp_threshold / api_threshold moved from a post-RRF-fusion cutoff (a rank
    artifact, see ranker.rank_hybrid) to a PER-ARM relevance floor — real
    cosine similarity for the vector arm, normalized BM25 for the FTS arm,
    applied before fusion. That changed what these two numbers mean, so both
    were re-measured against real bge-m3 embeddings + real SQLite FTS5 (no
    synthetic vectors), not carried over from the pre-fix values by default.
    Methodology (54 query/chunk pairs, three rounds, deliberately spanning
    the relevance range rather than stacking near-duplicates at cosine>=0.9):
    round 1 (n=30) full-sentence questions a user/agent would naturally ask;
    round 2 (n=18) short keyword-style queries; round 3 (n=6) exact
    identifiers/codes (IDs, CVEs, ticket refs) — adversarial to the vector
    arm on purpose, to see whether BM25 independently rescues.

    Vector-arm cosine for the TRUE match, combined across all three rounds
    (n=54): 100% clear 0.4, 98% clear 0.5 (single miss: a yes/no-phrased
    question sharing almost no vocabulary with its declarative target,
    cosine 0.480), only 74% clear 0.6, only 28% clear 0.7. Round-1-only
    (full-sentence questions — the realistic MCP/agent-caller shape): only
    60% clear 0.6. The distractor side: the SAME query's hardest wrong
    answer clears 0.6 in 0% of cases (perfect precision, but 26+ points of
    recall paid for it) vs 85% at 0.4 (very loose — precision is RRF/rank
    ordering's job here, not the floor's).

    Conclusion: api_threshold=0.6 measurably drops ~1 in 4 genuinely
    relevant results overall, and ~2 in 5 on natural-language queries
    specifically — exactly the "ask for 15, get 3" regression the semantic
    change risked if the old numeric values were kept unchanged. Lowered to
    0.5 (98% combined recall, still meaningfully stricter than mcp_threshold
    as originally intended). mcp_threshold=0.4 was ALREADY safe under the
    new semantics (100% recall in every round measured) and is unchanged.

    Known, measured, NOT fixed here: BM25-normalized and cosine are not on a
    comparable scale, so one shared threshold value is itself imprecise.
    FTS retrieved a candidate at all in only 17/54 pairs (0/30 for
    full-sentence queries — sanitize_fts_query's boolean-operator stripping
    plus FTS5's implicit-AND-across-all-terms means an ordinary question
    essentially never token-matches its target) and its own normalized score
    for a genuine hit skewed low even where BM25 should be doing the real
    work: single-identifier queries in round 3 scored 0.131-0.352, all below
    even mcp_threshold. In every round measured, whenever FTS DID retrieve
    the true match, the vector arm ALSO scored it >=0.5 — so at either
    current value, BM25's independent-rescue role is close to vestigial for
    the query shapes tested. A structurally correct fix (separate per-arm
    thresholds, or recalibrating search_fts's raw-score/25.0 normalization)
    is a bigger change than adjusting these two numbers and is intentionally
    not made here.
    """
    mcp_threshold: float = 0.4
    api_threshold: float = 0.5
    # The BEAM k-sweep (400 answers/point, replicated on a second judge family)
    # measured contradiction_resolution rising
    # 0.300→0.388→0.456 at k=5/10/15 then plateauing to k=25 (0.416, n.s. step).
    # k=10 sat on the rising part of the curve, not the plateau; the effect is
    # specific to contradiction detection (depth×system DiD +0.155, p=0.022;
    # a same-embedder dense-RAG baseline moved +0.010, p=0.768 over the same
    # depth increase) — not a generic "more context helps" result. Raised to
    # 15, the first plateau point; the last individually-significant step is
    # 5→15 (p=0.001), not 10→15 (p=0.140) — "10 is below the plateau" is the
    # supported claim, not "15 is optimal". Safe to raise now that the hybrid
    # search rank-locked ceiling (see ranker.rank_hybrid) no longer caps
    # results below this value.
    default_limit: int = 15
    #: Largest ``limit`` the HTTP surface will accept, so an absurd value gets a
    #: 422 naming the bound instead of being silently clamped. It is NOT what
    #: keeps sqlite-vec's KNN ceiling legal — ``store.VEC_KNN_MAX_K`` does that,
    #: because the multipliers between here and the query make an edge bound the
    #: wrong instrument (see that constant). Deliberately far wider than the MCP
    #: surface's 50: MCP is bounded for token cost, while the API serves the
    #: consolidation and wiki-maintenance passes that legitimately want wide
    #: recall.
    max_limit: int = 1000
    exclude_status: list[str] = field(default_factory=lambda: ["archived"])
    hybrid_weight: float = 0.5
    hybrid_enabled: bool = True
    dedup_score_gap: float = 0.2
    daily_penalty: float = 0.3  # Multiplier for daily/ files (0.3 = 30% of original score)
    # cap per-result body returned by /search via the `snippet` field.
    # MCP renders snippet by default; full chunk content remains available
    # through `content` (API/CLI) or the `full=true` flag on palinode_search.
    snippet_max_chars: int = 400

@dataclass
class ReadConfig:
    """Caps for the tiered read views.

    Tiers are computed at read time from content already in hand — these are
    presentation caps, not storage limits. Nothing here changes what is on
    disk or in the index.
    """
    #: ``tier=abstract`` — summary / canonical_question / first paragraph.
    abstract_max_chars: int = 300
    #: ``tier=overview`` — frontmatter block plus the head of the body.
    overview_max_chars: int = 4000

@dataclass
class NightlyConfig:
    """Lightweight daily update configurations."""
    enabled: bool = True
    lookback_days: int = 1
    allowed_ops: list[str] = field(default_factory=lambda: ["UPDATE", "SUPERSEDE", "MERGE"])

@dataclass
class WriteTimeConfig:
    """Tier 2a (ADR-004): write-time contradiction check on palinode_save.

    When enabled, every save schedules a background contradiction check
    against similar existing memories. The check runs asynchronously
    (via an asyncio queue in the API server, or disk-backed marker files
    from CLI/plugin paths) and never blocks the save caller. Errors in
    the check are logged but never propagate to the save response.

    Default disabled — flip to true after validating in a dev environment.
    """
    enabled: bool = False
    queue_max_size: int = 1000
    check_timeout_seconds: int = 30
    pending_dir: str = ".palinode/pending"
    sweep_on_startup: bool = True

@dataclass
class ForgetConfig:
    """Write-time forgetting: explicit "please forget X" → archival.

    When enabled, every save runs a deterministic forget-request detector on
    the incoming content; a hit resolves the named preference to stored
    memories via hybrid search and archives them, while the request memory
    itself stays active as the retrieval-visible retraction record. Silent
    full removal measured *worse than doing nothing*.

    Default disabled — flip on after validating against a real store; a
    resolution false-positive archives live memories (reversibly, but still).
    """
    enabled: bool = False
    # Hybrid-search candidates considered per request. Search is used for
    # RANKING only — post-RRF scores are rank artifacts, so there is no score
    # threshold here (see palinode/consolidation/forget.py).
    search_k: int = 10
    # Precision guards, both required: a candidate must share at least
    # min_shared_words content words with the pref phrase (drops unrelated
    # memories that rank on template similarity), and at most max_targets
    # survivors are archived (the validating measurement archived exactly the
    # two messages of the establishing exchange). Precision over recall: the
    # retained request memory covers what resolution misses.
    min_shared_words: int = 1
    max_targets: int = 2
    # Granularity router: forgetting is fact/entity-shaped but archival is
    # file-shaped, so a resolved target that merely *mentions* the pref inside
    # a dense shared memory would lose everything else in the file if archived
    # whole. A target archives only when at least this fraction of its content
    # words (whole file, not the matching chunk) are shared with the pref
    # phrase; below the floor the matching sentences are struck in place
    # instead (mention-level retraction, palinode/consolidation/retract.py)
    # and the rest of the file stays live. 0.0 disables the routing (every
    # resolved target archives whole).
    min_target_coverage: float = 0.05

@dataclass
class ConsolidationConfig:
    """Interval LLM job configuration settings logic."""
    enabled: bool = True
    schedule: str = "0 3 * * 0"  # Sunday 3am UTC
    lookback_days: int = 7
    # LLM for consolidation tasks (OpenAI-compatible API)
    llm_url: str = "http://localhost:8000"
    llm_model: str = "/model"
    llm_fallbacks: list[dict] = field(default_factory=list)
    llm_temperature: float = 0.3
    llm_max_tokens: int = 2000
    # Which ops the weekly/full pass (run_consolidation) may apply — the
    # counterpart to nightly.allowed_ops below, which restricts the nightly
    # pass only. One name, one nesting depth per pass: this key governs
    # weekly, `nightly.allowed_ops` governs nightly. There used to be a
    # third, unrelated `compaction.allowed_ops` key that looked like it did
    # this and did nothing — removed; this is now the only weekly-pass knob.
    allowed_ops: list[str] = field(default_factory=lambda:
        ["KEEP", "UPDATE", "MERGE", "SUPERSEDE", "ARCHIVE", "RETRACT"])
    nightly: NightlyConfig = field(default_factory=NightlyConfig)
    write_time: WriteTimeConfig = field(default_factory=WriteTimeConfig)
    forget: ForgetConfig = field(default_factory=ForgetConfig)
    keyword_map: dict[str, list[str]] | None = None
    # `### <date>` blocks kept verbatim in a status doc's Consolidation Log;
    # older blocks collapse into one cumulative elision line (the full detail
    # stays in git history). 0 disables the cap.
    status_log_max_blocks: int = 10

@dataclass
class DecayConfig:
    """Algorithm constraints matching temporal decay curves settings.

    ADR-007 (demand-decay importance, grounded in 31 d of prod telemetry)
    replaces the per-type decay model that used to live here with a single
    empirical demand-decay clock. ``importance`` is now *decayed
    distinct-session explicit demand*: reinforced by an exponential-approach
    nudge on each qualifying demand (§3.3), decayed on read at rank time
    (§3.3/§3.4). The per-type `tau_*` keys (pre-data guesses for that
    superseded model) were removed along with the rest of the dead config
    surface — their only reader, the legacy `score_with_decay` re-rank term,
    was itself deleted as dead code first.
    """
    enabled: bool = False
    # Recall-feedback loop (ADR-006/007) — demand-decay importance (ADR-007).
    # Access metadata (recall_count / last_recalled) is always written on
    # retrieval, independent of the decay ranker `enabled` flag (which gates the
    # bounded decay-on-read re-rank band in `rank_hybrid`). The *importance* nudge is gated
    # on explicit, session-deduplicated demand (§3.2) and reinforces by
    # exponential approach toward `importance_cap`:
    #     importance ← importance + (cap − importance) · importance_alpha
    # NULL importance is treated as `importance_base` (0.5) before nudging.
    importance_base: float = 0.5          # neutral prior (NULL ⇒ base); decay floor
    importance_cap: float = 0.95          # leaves headroom for human max (1.0)
    importance_alpha: float = 0.08        # reinforcement rate per distinct-session demand
    importance_tau_days: float = 14.0     # demand-decay time constant (decay-on-read)

@dataclass
class ApiServiceConfig:
    """FastAPI interface bind port schemas formats constraints."""
    host: str = "127.0.0.1"
    port: int = 6340
    log_level: str = "INFO"

@dataclass
class WatcherServiceConfig:
    """File tracking refresh schema configurations metrics."""
    debounce_seconds: float = 1.0

@dataclass
class ServicesConfig:
    """Nested configuration mapping array services configurations."""
    api: ApiServiceConfig = field(default_factory=ApiServiceConfig)
    watcher: WatcherServiceConfig = field(default_factory=WatcherServiceConfig)

@dataclass
class GitConfig:
    """Git logic auto execution formats limits inputs metrics."""
    auto_commit: bool = True
    auto_push: bool = False
    commit_prefix: str = "palinode"

@dataclass
class DoctorConfig:
    """Configuration for palinode doctor diagnostics.

    search_roots: directories to search for phantom .palinode.db files when
                  running the phantom_db_files check.  Each entry is an
                  absolute path string; ~ expansion is applied.

                  When empty (the default), the built-in plausible roots are
                  used (home, ~/palinode, ~/palinode-data, /var/lib/palinode,
                  and a few historical local paths).

                  When non-empty, ONLY the listed paths are searched — the
                  built-in list is bypassed entirely.  This lets operators pin
                  the exact set of roots on production hosts, and lets tests
                  isolate themselves to tmp_path directories without the check
                  discovering real databases elsewhere on the machine.
    """
    search_roots: list[str] = field(default_factory=list)


@dataclass
class AuditConfig:
    """MCP tool call audit logging for compliance and debugging."""
    enabled: bool = True
    log_path: str = ".audit/mcp-calls.jsonl"

@dataclass
class InstrumentationConfig:
    """Retrieval-event instrumentation (ADR-007 prerequisite, from the retrieval-event
instrumentation).

    capture_retrievals: write one JSONL event per file surfaced by search/read.
    Set to False (or PALINODE_INSTRUMENTATION_DISABLED=1) to suppress entirely.
    """
    capture_retrievals: bool = True

@dataclass
class LoggingConfig:
    """Log formatting and target directories constraints formats."""
    operations_log: str = "logs/operations.jsonl"
    console: bool = True

@dataclass
class LayerSplitConfig:
    """Heuristics for classifying markdown sections into Identity/Status/History layers.
    
    These keyword lists are intentionally configurable — they're guesses based on
    common heading patterns, not ground truth. Override in palinode.config.yaml
    when your files use different section naming conventions.
    
    Evolution strategy:
    - After running split-layers, inspect git diff to see what was classified correctly
    - Add/remove keywords based on what you observe  
    - Use `layer_hint: identity`, `layer_hint: status`, or `layer_hint: history`
      in file frontmatter to override the heuristic for specific files — the whole
      body moves to that layer's file
    - Over time these will converge on your actual naming conventions
    """
    # Section headings containing these words → Identity layer (slow-changing core facts)
    identity_keywords: list[str] = field(default_factory=lambda: [
        "architecture", "context", "people", "canon", "what this is",
        "key decisions", "overview", "about", "design", "stack",
        "key files", "follow-up", "who", "background", "principles",
    ])
    # Section headings containing these words → Status layer (fast-changing current state)
    status_keywords: list[str] = field(default_factory=lambda: [
        "current", "status", "milestone", "active", "this week",
        "open", "consolidation log", "todo", "in progress", "recent",
        "progress", "now", "today", "next", "blocking",
    ])
    # If no keyword match AND section body contains a date like 2026-03-xx → Status
    date_pattern: str = r"\d{4}-\d{2}-\d{2}"


@dataclass
class ContextConfig:
    """Ambient context for search boosting. Resolves caller's project from CWD."""
    enabled: bool = True
    boost: float = 1.5              # Multiplier for context-matching results (1.0 = disabled)
    auto_detect: bool = True        # Fall back to project/{basename(cwd)} if not in project_map
    project_map: dict[str, str] = field(default_factory=dict)  # CWD basename → entity ref
    embed_augment: bool = True      # Prepend project context to query before embedding

@dataclass
class AutoInjectConfig:
    """ADR-012 Layer 4: server-side session-start context for MCP clients.

    ``instructions_enabled`` puts a short, content-free memory contract into
    the MCP ``initialize`` response — every client sees it, and because it
    carries no memory content there is no scope-bleed risk. ``enabled`` is
    the master switch for the ``palinode_session_init`` digest tool.
    ``harnesses_disabled`` lists clientInfo-name substrings for harnesses
    that already have instruction-file/skill/hook layers and should not
    double-inject (Claude Code by default — CLAUDE.md, skills, and the
    SessionStart hook already cover it).

    The two interact: the instructions tell a client to call
    ``palinode_session_init`` only when that client would actually be served
    the digest. A suppressed harness — or any client, when ``enabled`` is
    false — is pointed at ``palinode_search`` instead, rather than being asked
    for a tool call the server then refuses.
    """
    enabled: bool = True
    instructions_enabled: bool = True
    harnesses_disabled: list[str] = field(default_factory=lambda: ["claude-code"])


@dataclass
class ScopeConfig:
    """ADR-009 Layer 1: scope chain for multi-harness, multi-agent, team memory.

    Scopes form an entity-ref hierarchy: org → member → project → harness → agent → session.
    Memories inherit DOWN the chain by default. A session's scope is resolved from
    env vars and config; see ADR-009 §3.2.

    Layer 1 scope (this slice): resolution only — produces a ScopeChain from
    config + env. Later slices wire the chain into store search, the
    /context/prime endpoint, and frontmatter `scope` field parsing.

    Env vars:
      PALINODE_ORG      → scope.org
      PALINODE_MEMBER   → scope.member
      PALINODE_HARNESS  → scope.harness  (MCP client auto-detection is Layer 2+)
      PALINODE_AGENT    → scope.agent    (multi-agent orchestration only)

    prime_mode:
      "classic" — /context/prime injects all core files regardless of scope.
      "scoped"  — /context/prime filters core files by the session's scope
                  chain (the default). Safe flip per ADR-009 §7: only memories
                  with *explicit* scope: frontmatter isolate, so scoped is
                  behavior-identical to classic until someone writes
                  harness/member-scoped memories.
    """
    enabled: bool = False
    org: str | None = None
    member: str | None = None
    harness: str | None = None
    agent: str | None = None
    prime_mode: str = "scoped"


@dataclass
class KUCompatConfig:
    """IETF Knowledge Unit (draft-farley-acta-knowledge-units) frontmatter alignment.

    When ``enabled`` is True, every save auto-populates the KU fields
    ``ku_version``, ``lifecycle``, ``content_hash``, and ``confidence`` (if
    provided by the caller) in the written frontmatter.

    When ``enabled`` is False (the default), KU fields are only written when
    the caller explicitly provides them — no auto-population. This preserves
    backward compatibility for deployments that don't need KU interoperability.

    ``ku_version`` is always ``"1.0"`` (the current draft revision).
    ``lifecycle`` mirrors ``status`` when present; defaults to ``"active"``.
    """
    enabled: bool = False
    ku_version: str = "1.0"


@dataclass
class CompactionConfig:
    """Layer-split heuristics for markdown section classification.

    ``allowed_ops`` and ``aggressiveness`` used to live here but had no
    reader anywhere in the tree — the ops filter a user actually gets is
    ``consolidation.allowed_ops`` (weekly) / ``consolidation.nightly.allowed_ops``
    (nightly). Removed rather than wired up: neither name nor scope matched
    what this section otherwise does (layer-split tuning), and duplicating
    the ops-restriction knob under a second name is exactly the ambiguity
    that made the first one silently unread.
    """
    layer_split: LayerSplitConfig = field(default_factory=LayerSplitConfig)

@dataclass
class Config:
    """Global configuration model mapping all schema structures format maps formats outputs."""
    memory_dir: str = "~/palinode"
    tool_surface: ToolSurface = "full"
    # Sentinel `None` means "default to memory_dir/.palinode.db, tracking
    # PALINODE_DIR overrides at load time". See __post_init__ + load_config.
    # An explicit string (e.g. from palinode.config.yaml) is taken at face value.
    db_path: str | None = None
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    ingestion: IngestionConfig = field(default_factory=IngestionConfig)
    embeddings: EmbeddingsConfig = field(default_factory=EmbeddingsConfig)
    auto_summary: AutoSummaryConfig = field(default_factory=AutoSummaryConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    read: ReadConfig = field(default_factory=ReadConfig)
    consolidation: ConsolidationConfig = field(default_factory=ConsolidationConfig)
    compaction: CompactionConfig = field(default_factory=CompactionConfig)
    ku_compat: KUCompatConfig = field(default_factory=KUCompatConfig)
    context: ContextConfig = field(default_factory=ContextConfig)
    auto_inject: AutoInjectConfig = field(default_factory=AutoInjectConfig)
    scope: ScopeConfig = field(default_factory=ScopeConfig)
    decay: DecayConfig = field(default_factory=DecayConfig)
    services: ServicesConfig = field(default_factory=ServicesConfig)
    git: GitConfig = field(default_factory=GitConfig)
    audit: AuditConfig = field(default_factory=AuditConfig)
    instrumentation: InstrumentationConfig = field(default_factory=InstrumentationConfig)
    doctor: DoctorConfig = field(default_factory=DoctorConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    
    @property
    def palinode_dir(self) -> str:
        return self.memory_dir

    def __post_init__(self):
        # Support ~ expansion in specific paths
        self.memory_dir = _expand_path(self.memory_dir)
        # Handle db_path absolute or relative.
        # `None` is the sentinel meaning "default to memory_dir/.palinode.db";
        # leave it for load_config() to resolve AFTER env-var overrides apply.
        # If the user passed an explicit string, normalize it now: relative
        # paths land under memory_dir, absolute paths stay as-is.
        if self.db_path is not None and not os.path.isabs(self.db_path):
            self.db_path = os.path.join(self.memory_dir, self.db_path)

    def validate_paths(self) -> list[str]:
        """Return human-readable warning strings for path misconfigurations.

        Checks:
          (a) memory_dir exists on disk
          (b) db_path parent directory exists on disk
          (c) db_path is under memory_dir (warns if not — not an error by itself)

        An empty return list means all checks passed.  Callers should log each
        entry at WARNING level; a missing db_path parent is the only condition
        serious enough to refuse startup (the caller decides policy).
        """
        warnings: list[str] = []

        memory_dir = Path(self.memory_dir).resolve()
        db_path = Path(self.db_path).resolve()
        db_parent = db_path.parent

        if not memory_dir.exists():
            warnings.append(
                f"memory_dir does not exist: {memory_dir}"
            )

        if not db_parent.exists():
            warnings.append(
                f"db_path parent directory does not exist: {db_parent} "
                f"(db_path={db_path})"
            )

        try:
            db_path.relative_to(memory_dir)
        except ValueError:
            warnings.append(
                f"db_path is outside memory_dir — they may have diverged. "
                f"memory_dir={memory_dir}  db_path={db_path}. "
                f"If you moved the data directory and updated PALINODE_DIR, "
                f"also update db_path in palinode.config.yaml."
            )

        return warnings


def _deep_merge(target: dict, source: dict) -> dict:
    """Deep merge two dictionaries."""
    for key, value in source.items():
        if isinstance(value, dict):
            node = target.setdefault(key, {})
            _deep_merge(node, value)
        else:
            target[key] = value
    return target


def load_config() -> Config:
    """Loads configuration from yaml files and environment variables."""
    # Base defaults
    raw_config = {}
    
    # 1. and 2. Resolve Config YAMLs
    repo_root_config = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "palinode.config.yaml"))
    
    default_palinode_dir = os.environ.get("PALINODE_DIR", os.path.expanduser("~/palinode"))
    palinode_dir_config = os.path.join(default_palinode_dir, "palinode.config.yaml")
    
    config_paths = [repo_root_config, palinode_dir_config]
    loaded_path = None
    
    for cpath in config_paths:
        if os.path.exists(cpath):
            try:
                with open(cpath, 'r', encoding="utf-8") as f:
                    file_conf = yaml.safe_load(f) or {}
                    _deep_merge(raw_config, file_conf)
                loaded_path = cpath
            except Exception as e:
                # A corrupt/unreadable config that silently falls back to
                # built-in defaults is the failure class. Route through the
                # logger (not print→stderr, which bypasses log capture) so the
                # fallback is greppable on the host.
                _logger.warning(
                    "failed to load config; continuing with remaining sources/defaults "
                    "op=config_load path=%s error=%r",
                    cpath, str(e),
                )

    # Initialize dataclass with Pydantic validation
    try:
        adapter = TypeAdapter(Config)
        cfg = adapter.validate_python(raw_config)
    except ValidationError as e:
        # Validation failure aborts startup — make sure it hits the log before
        # the raise propagates, not only stderr.
        _logger.error("failed to validate configuration op=config_validate error=%r", str(e))
        raise

    # 4. Environment variable overrides
    if "PALINODE_DIR" in os.environ:
        cfg.memory_dir = _expand_path(os.environ["PALINODE_DIR"])
        # If the user did not set db_path explicitly (sentinel `None`), it
        # remains None here and gets resolved against the post-env memory_dir
        # in step 5. If they did set it (YAML), preserve their intent: only
        # rebase when it's a bare relative path (originally a basename).
        if cfg.db_path is not None and not os.path.isabs(cfg.db_path):
            cfg.db_path = os.path.join(cfg.memory_dir, os.path.basename(cfg.db_path))

    # 5. Resolve sentinel db_path. Always tracks the final memory_dir, so
    #    `PALINODE_DIR=/tmp/foo` (with no YAML db_path) lands the DB at
    #    /tmp/foo/.palinode.db rather than the install-dir default.
    if cfg.db_path is None:
        cfg.db_path = os.path.join(cfg.memory_dir, ".palinode.db")

    # 6. Resolve audit.log_path: if still at the relative default, anchor it
    #    under memory_dir so every fresh install gets a consistent absolute
    #    path. Explicit absolute paths in user config are left untouched.
    #    Explicit *relative* paths set by the user still warn (the doctor
    #    check detects relative paths regardless of origin).
    _AUDIT_LOG_DEFAULT = ".audit/mcp-calls.jsonl"
    if cfg.audit.log_path == _AUDIT_LOG_DEFAULT:
        cfg.audit.log_path = os.path.join(cfg.memory_dir, ".audit", "mcp-calls.jsonl")
    if "OLLAMA_URL" in os.environ:
        cfg.embeddings.primary.url = os.environ["OLLAMA_URL"]
    if "EMBEDDING_MODEL" in os.environ:
        cfg.embeddings.primary.model = os.environ["EMBEDDING_MODEL"]
    if "PALINODE_API_HOST" in os.environ:
        cfg.services.api.host = os.environ["PALINODE_API_HOST"]
    if "PALINODE_API_PORT" in os.environ:
        try:
            cfg.services.api.port = int(os.environ["PALINODE_API_PORT"])
        except ValueError:
            # A malformed port silently ignored leaves the operator on the
            # default port wondering why their override didn't take.
            _logger.warning(
                "ignoring malformed env override; keeping configured value "
                "var=PALINODE_API_PORT value=%r",
                os.environ["PALINODE_API_PORT"],
            )
    if "PALINODE_MCP_SURFACE" in os.environ:
        cfg.tool_surface = validate_tool_surface(
            os.environ["PALINODE_MCP_SURFACE"], "PALINODE_MCP_SURFACE"
        )
    if "PALINODE_ORG" in os.environ:
        cfg.scope.org = os.environ["PALINODE_ORG"]
    if "PALINODE_MEMBER" in os.environ:
        cfg.scope.member = os.environ["PALINODE_MEMBER"]
    if "PALINODE_HARNESS" in os.environ:
        cfg.scope.harness = os.environ["PALINODE_HARNESS"]
    if "PALINODE_AGENT" in os.environ:
        cfg.scope.agent = os.environ["PALINODE_AGENT"]
    if "PALINODE_DESCRIBE_TIMEOUT_SECONDS" in os.environ:
        try:
            cfg.auto_summary.describe_timeout_seconds = float(
                os.environ["PALINODE_DESCRIBE_TIMEOUT_SECONDS"]
            )
        except ValueError:
            # Malformed timeout silently ignored keeps the default describe
            # timeout, masking a tuning attempt.
            _logger.warning(
                "ignoring malformed env override; keeping configured value "
                "var=PALINODE_DESCRIBE_TIMEOUT_SECONDS value=%r",
                os.environ["PALINODE_DESCRIBE_TIMEOUT_SECONDS"],
            )

    # Warn if PALINODE_DIR is set but db_path was not updated to match
    if "PALINODE_DIR" in os.environ:
        memory_dir = os.path.abspath(os.path.expanduser(os.environ["PALINODE_DIR"]))
        db_path = os.path.abspath(cfg.db_path)
        try:
            Path(db_path).relative_to(memory_dir)
        except ValueError:
            _logger.warning(
                "PALINODE_DIR is set but db_path does not fall under it — "
                "they may have diverged after a directory rename. "
                "memory_dir=%s  db_path=%s. "
                "Update db_path in palinode.config.yaml to suppress this warning.",
                memory_dir,
                db_path,
            )

    # Print summary string
    try:
        num_files = len(glob.glob(os.path.join(cfg.memory_dir, "**/*.md"), recursive=True))
    except (OSError, ValueError):
        num_files = 0

    # when defaults are loaded, surface the fact LOUDLY — a dim "defaults"
    # label is easy to miss when systemd wires PALINODE_DIR but an interactive
    # ssh session doesn't. Production deployments hit this when humans invoke
    # `palinode lint`, ad-hoc cron, or `claude --mcp` outside the systemd unit
    # env and silently run against the wrong filesystem. Two-pronged signal:
    #   1. A "⚠ defaults" prefix in the banner.
    #   2. A logger.warning listing every path we searched, so the user can
    #      see exactly where to drop a config file.
    if loaded_path is None:
        _logger.warning(
            "no palinode.config.yaml found — using built-in defaults. "
            "Searched: %s. Set PALINODE_DIR or place a config file in one of "
            "these locations to suppress this warning.",
            ", ".join(config_paths),
        )

    # Diagnostic banner — write to stderr so machine-readable stdout
    # (e.g. `palinode doctor --json | jq`) stays clean. Per Unix convention,
    # informational/diagnostic output belongs on stderr.
    banner_label = "⚠ defaults (no config file found)" if loaded_path is None else loaded_path
    print(
        f"Palinode config: {banner_label} "
        f"({num_files} files, {cfg.embeddings.primary.model} @ {cfg.embeddings.primary.url})",
        file=sys.stderr,
    )

    return cfg


# Singleton config instance
config = load_config()
