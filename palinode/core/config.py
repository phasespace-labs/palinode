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

# Wire dialects the embed client can speak. "ollama" is the native
# /api/embed (+ legacy /api/embeddings) pair; "openai" is the OpenAI-compatible
# /v1/embeddings shape served by llama.cpp (`llama-server --embedding`), vLLM,
# and LM Studio. Validated at load time so a typo fails loud instead of
# silently falling back to the Ollama wire format.
VALID_EMBEDDING_DIALECTS: set[str] = {"ollama", "openai"}


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
class TranscriptCaptureConfig:
    """Opt-in mining of harness session transcripts for user corrections.

    Off by default and empty by default: with no ``harness_paths`` entry there is
    nothing to read even when ``enabled`` is true. Transcripts are the most
    sensitive thing Palinode can be pointed at, so enabling the source and naming
    the directories are two separate, explicit operator acts.

    Keys of ``harness_paths`` are harness ids with a reader
    (``palinode.corrections.readers``); values are absolute directories, e.g.
    ``{"claude-code": ["~/.claude/projects"]}``. Everything read is bounded by
    ``lookback_days`` and ``max_candidates``, and what is skipped is counted in
    the scan report rather than dropped quietly.

    ``classify`` is the third act, and the only one that transmits anything.
    Detection is local arithmetic over files; classification sends a bounded
    window of conversation text to the configured consolidation endpoint, which
    may be a remote host. Defaulting it to ``False`` means turning the source on
    never, by itself, causes a byte of transcript to leave the machine — with it
    off a scan runs the deterministic stage only and every candidate is queued
    for review. There is deliberately no per-call override anywhere: a caller
    able to switch it on for one request could send out text the operator's
    config said stays local.
    """
    enabled: bool = False
    harness_paths: dict[str, list[str]] = field(default_factory=dict)
    lookback_days: int = 7
    max_candidates: int = 50
    classify: bool = False

@dataclass
class CaptureConfig:
    """General capture capability configuration map.

    ``cross_refs`` and ``transcripts`` are live — session extraction, daily-note
    capture, and quick-capture are all handled by their respective callers with
    no config read-through, so no dataclasses exist here for them.
    """
    cross_refs: CrossRefsConfig = field(default_factory=CrossRefsConfig)
    transcripts: TranscriptCaptureConfig = field(default_factory=TranscriptCaptureConfig)

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
    # Wire protocol at `url`. "ollama" (default; back-compat) = native
    # /api/embed with the /api/embeddings fallback. "openai" = POST
    # {model, input} to /v1/embeddings (llama.cpp, vLLM, LM Studio) — set this
    # when the embedding host is not Ollama. Same retry/backoff, circuit
    # breaker, and typed per-input errors either way. Mirrors the CHAT role's
    # `auto_summary.api` selector; no auto-detection.
    dialect: str = "ollama"
    # Optional path for OpenAI-compatible embedding providers whose endpoint is
    # not /v1/embeddings. None preserves the legacy /v1/embeddings behaviour.
    endpoint_path: str | None = None
    # Ollama's GPU path for GGUF bge-m3 returns a NaN vector for a small set of
    # exact inputs (llama.cpp casts K/V to F16 before flash attention on
    # cacheless encoders; Inf → NaN in softmax; the server refuses to serialise
    # it, HTTP 500). The same input embeds correctly on the CPU path. When set,
    # a NaN-shaped rejection is retried once with ``options.num_gpu: 0`` and
    # ``keep_alive: 0`` — the second is load-bearing: under a long server-side
    # keep_alive a CPU request would leave the model CPU-resident for every
    # later caller (seen on the shared host, 2026-09-08). Ollama dialect only.
    # Cost: one CPU load + embed + unload per rare input. Off → the chunk stays
    # FTS-only until re-embedded, as before.
    nan_cpu_retry: bool = True

    def __post_init__(self) -> None:
        normalized = self.dialect.strip().lower()
        if normalized not in VALID_EMBEDDING_DIALECTS:
            raise ValueError(
                "embeddings.primary.dialect must be one of "
                f"{sorted(VALID_EMBEDDING_DIALECTS)}, got {self.dialect!r}"
            )
        self.dialect = normalized

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
class EvidenceConfig:
    """Budgets for the opt-in evidence resolver behind ``search --resolve``.

    Each is a hard, deterministic ceiling on one kind of work the resolver
    may do for one request (``palinode.core.evidence``). Link traversal
    (``max_files`` / ``max_edges`` / ``max_depth``), replacement-chain walks
    (``max_replacement_chain``) and unlinked discovery (``fallback_*``) are
    budgeted separately so exhausting one never silently starves another,
    and every exhausted budget is named in the result's ``coverage``.
    Conservative on purpose: the resolver runs inside a search request.
    """
    #: Distinct files read from disk beyond the seeds (link traversal).
    max_files: int = 24
    #: Typed-link edges followed (forward and reverse, all seeds together).
    max_edges: int = 48
    #: Hops of ``contradicts`` / ``backed_by`` from a seed.
    max_depth: int = 2
    #: Hops along a ``superseded_by`` chain from any node, kept apart from
    #: ``max_depth`` so a long replacement lineage cannot eat the edge budget.
    max_replacement_chain: int = 8
    #: Retrieval calls unlinked discovery may make (entity, keyword, neighbour
    #: lookups count one each), spent in seed rank order.
    fallback_max_queries: int = 18
    #: Files unlinked discovery may read beyond the linked ones.
    fallback_max_reads: int = 12
    #: Hops of ``backed_by`` the read-time support check walks from a record
    #: (1 = its own sources, 2 = its sources' sources). Separate from
    #: ``max_depth`` so a spent traversal budget cannot silence the second-hop
    #: check; its file reads are still charged against ``max_files``.
    max_support_hops: int = 2

@dataclass
class SearchConfig:
    """Matching index score cutoffs thresholds layouts.

    mcp_threshold / api_threshold moved from a post-RRF-fusion cutoff (a rank
    artifact, see ranker.rank_hybrid) to a pre-fusion vector relevance floor
    measured as real cosine similarity. That changed what these two numbers
    mean, so both were re-measured against real bge-m3 embeddings + real
    SQLite FTS5 (no synthetic vectors), not carried over from the pre-fix
    values by default. BM25 uses the independent ``fts_threshold`` below.
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

    Known, measured, and since fixed elsewhere: BM25-normalized and cosine are
    not on a comparable scale, so one shared threshold value is itself
    imprecise. When these bands were measured, FTS retrieved a candidate at all
    in only 17/54 pairs (0/30 for full-sentence queries — FTS5's implicit AND
    across every token meant an ordinary question never matched its target);
    that half is fixed by ``store.fts_match_expression`` (OR-joined
    content words, identifier phrases), measured at +5.8 on exact-label
    questions. The other half was the arm's normalized score skewing low
    exactly where BM25 should do the real work — single-identifier queries in
    round 3 scored 0.131-0.352, all below even mcp_threshold. Both halves of
    that are now addressed: the FTS arm has its own floor (``fts_threshold``
    below, relative), and ``store.bm25_query_scale`` normalizes each query's
    BM25 against what that query could score in this index rather than against
    a constant 25, so an exact identifier hit scores ~1.0 on any store size.
    These two values are cosine floors and were not touched by either.
    """
    mcp_threshold: float = 0.4
    api_threshold: float = 0.5
    # The FTS arm's own floor, RELATIVE to the best keyword match in the same
    # result set: an FTS candidate survives when ``score >= fts_threshold *
    # top_score``. ``threshold`` (mcp_/api_) is an absolute cosine floor; the
    # FTS arm's normalized BM25 is on a different scale, so an absolute floor
    # there discarded most correct keyword hits before fusion at 0.4/0.5.
    # Measured 2026-09-08 on the 54-pair rig after the OR-join
    # fix: the true chunk is the top keyword match in 51/54 pairs and within
    # 0.49–0.92× of it in the other three; the best distractor sits at a
    # median 0.39× of the top. 0.4 keeps every true hit in that set and drops
    # about half the distractors; rank fusion and top_k do the rest.
    # Unaffected by the per-query rescale of that score
    # (``store.bm25_query_scale``): every candidate of one query is divided by
    # the same positive number, so the ratio this floor reads is identical.
    fts_threshold: float = 0.4
    # The VECTOR arm's floor, relative to the best cosine in the same candidate
    # set: a candidate survives when ``cosine >= vector_relative_floor *
    # top_cosine``. Same shape as ``fts_threshold``, and for the same reason —
    # ``mcp_threshold`` / ``api_threshold`` are ABSOLUTE cosine floors, so they
    # say whether a candidate is plausible at all and nothing about whether it
    # is plausible *next to the match this query actually found*. Without a
    # relative cutoff the arm hands fusion every one of its ``top_k * 2``
    # candidates that clears the absolute floor, and weak neighbours fill every
    # remaining slot. ``0.0`` restores that.
    #
    # Calibrated from the recorded per-result cosines of the relevance rig's
    # hybrid runs (bench/relevance, corpus v1, question set v1, 62 questions,
    # top_k 5, real bge-m3, both shipped floors). Of the 52 relevant results
    # delivered at the 0.40 floor, the LOWEST ratio one carried against the best
    # cosine in its own slate was 0.884; the tenth percentile sat at 0.932 and
    # the median at 1.000. Irrelevant results overlap that range but skew well
    # below it (median 0.920, lower quartile 0.851, minimum 0.675). 0.85 is the
    # last round value under the weakest true hit, with the same ~4-point margin
    # the keyword arm's floor keeps: 0.88 would sit 0.004 from losing a relevant
    # result and 0.90 measurably loses two.
    #
    # Then run end to end on the same rig at 0.0 and at 0.85, hybrid arm, real
    # bge-m3: results delivered 304 -> 262 and injections 224 -> 186 at the 0.40
    # cosine floor (277 -> 240 and 198 -> 164 at 0.50), payload 502 -> 429 tokens
    # per question, useful-context 20.1% -> 23.5%, with recall 52/52, top-1 37/48
    # and correct abstention all unchanged and no question worse on relevant hits,
    # first-relevant rank or top-1. The useful-token count is identical either
    # way: what the floor removes is the part of the payload that was not the
    # answer.
    vector_relative_floor: float = 0.85
    # The keyword arm's floor when it is the ONLY arm: explicit lexical
    # retrieval (``retrieval_mode: lexical``) and the per-input keyword
    # fallback, where there is no vector arm to admit a candidate the keyword
    # arm would have dropped. Same relative shape as ``fts_threshold`` (a
    # fraction of the best keyword match in the same result set); a separate
    # number because a single-arm slate has no second opinion, so its floor has
    # to be the looser of the two. ``0.0`` restores the pre-0.22 behaviour, in
    # which this path applied NO floor at all and therefore filled ``top_k``
    # unconditionally.
    #
    # Measured on the relevance rig (bench/relevance, corpus v1, question set
    # v1, 62 questions, top_k 5), from the same per-question data the
    # 2026-09-20 baseline was recorded against. Across the 48 relevant results
    # delivered, the LOWEST ratio a relevant hit carried was 0.387; the next
    # two were 0.495 and 0.498. Irrelevant results sit far below that: 10% of
    # them score 0.0 against the top match and 30% are under 0.276. 0.35 is
    # the last value below the weakest true hit — 0.4 (the two-arm value)
    # measurably drops it.
    lexical_fts_threshold: float = 0.35
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
    retrieval_mode: Literal["hybrid", "lexical"] = "hybrid"
    hybrid_enabled: bool = True
    dedup_score_gap: float = 0.2
    # How many chunks from ONE source file may occupy the delivered slate while
    # chunks from other files are still competitive. Overflow is not discarded:
    # it is deferred to the back of the queue and still delivered when the
    # slate would otherwise come back short, so the cap changes *which* results
    # fill the slate, never how many. ``0`` = unlimited, the pre-0.22
    # behaviour, where ``dedup_score_gap`` alone let one file take 3 of 5 slots
    # on a small slate (the post-fusion scores it compares are rank-derived, so
    # adjacent ranks sit far inside the gap and nothing is suppressed).
    #
    # 1 rather than 2 because the rig measured 1 better on every counted
    # outcome (bench/relevance, corpus v1, question set v1, keyword arm, 62
    # questions, top_k 5, at the 0.35 floor): relevant-hit recall 49/52 vs
    # 48/52, same-file repeats 2 vs 6 of 200 delivered, useful-context tokens
    # 26.3% vs 25.7%, injections 128 vs 129, top-1 and project isolation
    # identical. The recall difference is one question — a four-section weekly
    # note took 3 of 5 slots and buried one of the two records that answered
    # it; capping the note let that record in.
    max_chunks_per_file: int = 1
    daily_penalty: float = 0.3  # Multiplier for daily/ files (0.3 = 30% of original score)
    # cap per-result body returned by /search via the `snippet` field.
    # MCP renders snippet by default; full chunk content remains available
    # through `content` (API/CLI) or the `full=true` flag on palinode_search.
    snippet_max_chars: int = 400
    #: Withhold the slate on the MCP surface when the delivery's confidence
    #: verdict (:mod:`palinode.core.confidence`) is ``none`` — nothing
    #: delivered reaches even the low mark on either pre-fusion arm.
    #:
    #: **Off by default, deliberately.** The verdict ships as a signal, not a
    #: filter: the measured failure this addresses is a caller treating weak
    #: material as an answer, and the fix for that is telling it the material
    #: is weak, not hiding the store. An empty slate also cannot be
    #: distinguished by the reader from an empty *store*, so switching this on
    #: trades one unanswerable question for another. Verdict, banner, receipt
    #: and log are unaffected by it either way, and every other surface
    #: (REST, CLI, inspector, plugin) keeps returning its rows — this is a
    #: presentation choice on the one surface whose reader is a model.
    abstain_on_no_confident_match: bool = False
    # Budgets for the opt-in evidence resolver (``resolve`` on search).
    evidence: EvidenceConfig = field(default_factory=EvidenceConfig)

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
    # The nightly's **catch-up bound**, not a window. Selection is a
    # per-project watermark (the timestamp of the last pass that resolved that
    # project), so this number no longer decides what a healthy run sees — it
    # decides how far back a cold or long-failed mark may reach, which is what
    # stops one abandoned project handing the model months of notes in a single
    # request. The cron's `--days N` overrides it for that run, with the same
    # meaning.
    #
    # 7 because `auto_gate.max_hours_elapsed` is 168: the gate may legitimately
    # let a week pass before firing a pass at its ceiling, and a bound shorter
    # than the ceiling would drop the notes the gate itself chose to wait on.
    # It was 1 while this was a window, which as a bound would have meant a
    # single failed night still lost a day — the property the watermark exists
    # to remove.
    lookback_days: int = 7
    # How many prompts one pass may send a project whose selection does not
    # fit one prompt. Each prompt is the same fixed-size contiguous excerpt,
    # resumed where the last one stopped; the pass stops early when nothing is
    # pending or a prompt fails, so this multiplies how much a night can clear
    # without growing any single call. 1 is one prompt per project per pass.
    max_prompts_per_project: int = 4
    # PROPOSE_CONTRADICTS is in the default set because it is the
    # no-winner counterpart to SUPERSEDE: it records a conflict in
    # frontmatter and retires nothing, so it is additive in exactly the
    # sense this restricted pass requires. Omitting it would filter out
    # every proposal the nightly prompt now asks for.
    allowed_ops: list[str] = field(default_factory=lambda:
        ["UPDATE", "SUPERSEDE", "MERGE", "PROPOSE_CONTRADICTS"])

@dataclass
class WriteTimeConfig:
    """Tier 2a (ADR-004): write-time contradiction check on palinode_save.

    When enabled, every save schedules a background contradiction check
    against similar existing memories. The check runs asynchronously
    (via an asyncio queue in the API server, or disk-backed marker files
    from CLI/plugin paths) and never blocks the save caller. Errors in
    the check are logged but never propagate to the save response.

    A disk-backed job's marker is retired only when the job reports success
    or when ``max_attempts`` handoffs have failed to produce one. The count
    lives in the marker file, so it survives a restart.

    Default disabled — flip to true after validating in a dev environment.
    """
    enabled: bool = False
    queue_max_size: int = 1000
    check_timeout_seconds: int = 30
    pending_dir: str = ".palinode/pending"
    sweep_on_startup: bool = True
    max_attempts: int = 3

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
class AutoGateConfig:
    """Activity gate for the automatic (cron) consolidation path.

    The wall-clock schedule alone gets both cases wrong: an idle week still
    burns an LLM pass, and a heavy day still waits for the next tick. An
    automatic pass runs only when **both** conditions hold — at least
    ``min_hours_elapsed`` since that pass last ran, and at least
    ``min_sessions`` session-end entries recorded since then — so the cron can
    fire as often as you like and the pass lands on use rather than on the
    calendar. The elapsed floor carries one hour of slack and is measured from
    the previous pass's *start*, so a daily cron satisfies the 24 h default
    despite tick jitter and however long the pass itself took; the ceiling has
    no slack.

    ``max_hours_elapsed`` is the ceiling that defeats the gate: past it the
    pass runs whatever the session count is. Without it, a store that ingests
    through the watcher and records no sessions would never consolidate — the
    dual gate turns "no sessions" into "never", which is worse than the wasted
    pass it exists to prevent. The default equals the weekly cadence
    consolidation already had, so an idle deployment keeps today's behaviour.

    Enabled by default, and only on the automatic path: ``palinode
    consolidate`` / ``dream``, ``POST /consolidate`` and the MCP tool bypass
    the gate unless they ask for it (``--respect-gate`` / ``respect_gate``).
    """
    enabled: bool = True
    min_hours_elapsed: float = 24
    min_sessions: int = 5
    max_hours_elapsed: float = 168

@dataclass
class ConsolidationConfig:
    """Interval LLM job configuration settings logic."""
    enabled: bool = True
    schedule: str = "0 3 * * 0"  # Sunday 3am UTC
    # Matches the crontab in docs/OPERATIONS.md and this module's own example.
    # The weekly is a deep clean (ARCHIVE/MERGE) over recent notes, not a safety
    # net for old ones: a note the nightly never consolidated is not revisited
    # once it falls outside this window.
    lookback_days: int = 3
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
        ["KEEP", "UPDATE", "MERGE", "SUPERSEDE", "ARCHIVE", "RETRACT",
         "PROPOSE_CONTRADICTS", "ARCHIVE_BEFORE"])
    nightly: NightlyConfig = field(default_factory=NightlyConfig)
    auto_gate: AutoGateConfig = field(default_factory=AutoGateConfig)
    write_time: WriteTimeConfig = field(default_factory=WriteTimeConfig)
    forget: ForgetConfig = field(default_factory=ForgetConfig)
    keyword_map: dict[str, list[str]] | None = None
    # `### <date>` blocks kept verbatim in a status doc's Consolidation Log;
    # older blocks collapse into one cumulative elision line (the full detail
    # stays in git history). 0 disables the cap.
    status_log_max_blocks: int = 10
    # How long a dated `- [YYYY-MM-DD] …` status log line stays in the document
    # before the weekly pass retires it to `-history.md` — deterministically,
    # with no model involved. One line per session accumulates faster than any
    # proposal can retire it: at 449 facts the honest ARCHIVE-per-fact proposal
    # overflowed every workable token cap, so nothing was ever retired.
    # 90 days is a quarter: long enough that a line is still in the window
    # while anyone might reasonably recall the session that wrote it, and
    # more than twelve times the weekly pass's own 7-day lookback, so a line
    # has been seen by a dozen passes before age alone retires it. Nothing is
    # lost — the sibling `-history.md` keeps every retired line verbatim.
    # 0 disables the sweep entirely. Only applied to age-eligible documents
    # (ADR-020): an identity/profile document is never retired by age.
    status_log_retention_days: int = 90

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
class WriteConfig:
    """What the capture surfaces normalize on the way in.

    ``normalize_relative_dates`` defaults ON because ``PROGRAM.md`` already
    requires it of every extractor: a relative time expression is resolved
    against the session date and stored absolute, because nothing downstream
    can recover which Tuesday "last Tuesday" was. Turning it off keeps the
    author's wording and leaves the drift for ``palinode lint`` to report.
    """
    normalize_relative_dates: bool = True

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
    """Ambient context for search boosting. Resolves caller's project from CWD.

    Also carries the **injection budgets** — the ceilings on what Palinode puts
    into a context window without being asked. The two surfaces are budgeted
    separately because they are paid differently: the startup payload is paid
    once per session and can afford orientation; the per-turn recall block is
    paid on every message and competes with the user's own turn.

    Both are expressed twice, in characters and in estimated tokens
    (``packing.estimate_tokens``, chars/4 — an estimate, not a tokenizer).
    Under that estimator the pairs below are two views of one ceiling; they
    diverge only if a real tokenizer ever replaces the estimate, which is why
    both are enforced. ``0`` disables a cap; ``0`` on both members of a pair
    leaves that surface bounded only by its own line/count limits
    (``context_prime.MAX_*``), which is exactly the pre-budget behaviour.

    Defaults. ``injection_max_chars = 6000`` (~1500 estimated tokens) is a
    little above what today's bounds can produce — 23 rows capped at
    ``MAX_LINE_CHARS`` plus headings — so a normal digest is unaffected and an
    accreting core set is caught instead of quietly crowding the window.
    ``recall_max_chars = 3000`` (~750 tokens) matches the per-turn ceiling the
    shipped harness plugin already applies (``PALINODE_HOOK_RECALL_MAX_CHARS``),
    so the server-side budget agrees with the client-side one rather than
    fighting it.
    """
    enabled: bool = True
    boost: float = 1.5              # Multiplier for context-matching results (1.0 = disabled)
    auto_detect: bool = True        # Infer from git identity, else normalized cwd basename
    project_map: dict[str, str] = field(default_factory=dict)  # Directory/repository name → entity ref
    embed_augment: bool = True      # Prepend project context to query before embedding
    #: Session-start core injection: /context/prime, palinode_session_init,
    #: palinode prime.
    injection_max_chars: int = 6000
    injection_max_tokens: int = 1500
    #: Per-turn recall block (the harness recall hook's payload).
    recall_max_chars: int = 3000
    recall_max_tokens: int = 750
    #: A `core: true` memory is an index entry — a gist and a pointer to the
    #: file that holds the detail. Above this size it is a document wearing a
    #: core flag, and `palinode lint` says so. 1500 chars (~375 estimated
    #: tokens, roughly a screenful) keeps the whole core set readable in a few
    #: thousand tokens even when every pointer is followed.
    core_gist_max_chars: int = 1500

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
    write: WriteConfig = field(default_factory=WriteConfig)
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
    if "PALINODE_RETRIEVAL_MODE" in os.environ:
        mode = os.environ["PALINODE_RETRIEVAL_MODE"].strip().lower()
        if mode not in {"hybrid", "lexical"}:
            raise ValueError("PALINODE_RETRIEVAL_MODE must be hybrid or lexical")
        cfg.search.retrieval_mode = mode
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
        f"({num_files} files, embedding model {cfg.embeddings.primary.model})",
        file=sys.stderr,
    )

    return cfg


# Singleton config instance
config = load_config()
