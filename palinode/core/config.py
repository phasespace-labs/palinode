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

import os
import glob
from pathlib import Path
from dataclasses import field
from pydantic.dataclasses import dataclass
from pydantic import TypeAdapter, ValidationError
import yaml

def _expand_path(path_str: str) -> str:
    """Expand ~ and normalizes path."""
    return os.path.expanduser(path_str)

@dataclass
class CoreRecallConfig:
    """Settings for core memory injection logic."""
    max_chars_per_file: int = 3000
    max_total_chars: int = 8000
    directories: list[str] = field(default_factory=lambda: ["people", "projects", "decisions", "insights"])

@dataclass
class TieringRecallConfig:
    """Configures tiered memory recall execution."""
    full_refresh_every_n_turns: int = 200  # Fallback only — compaction hook is primary trigger
    skip_unsummarized: bool = True
    # What to inject on non-full turns (between refreshes):
    #   "none"     — skip core entirely (just topic search). Saves ~200 tokens/turn.
    #   "summary"  — inject one-line summaries of core files.
    #   "full"     — inject full core every turn (expensive, ~3K tokens/turn).
    mid_turn_mode: str = "none"

@dataclass
class SearchRecallConfig:
    """Search logic tuning boundaries and constraints."""
    enabled: bool = True
    top_k: int = 3          # Was 5 — 3 high-quality results beat 5 noisy ones
    max_chars_per_chunk: int = 500  # Was 700 — tighter excerpts, less noise
    threshold: float = 0.4
    min_query_length: int = 15
    trivial_patterns: list[str] = field(default_factory=lambda: [
        "ok", "yep", "sure", "thanks", "thx", "cool", "got it", "nice", "lol", "k", "np"
    ])

@dataclass
class RecallConfig:
    """Holistic recall mechanism toggle layout."""
    enabled: bool = True
    core: CoreRecallConfig = field(default_factory=CoreRecallConfig)
    tiering: TieringRecallConfig = field(default_factory=TieringRecallConfig)
    search: SearchRecallConfig = field(default_factory=SearchRecallConfig)

@dataclass
class ExtractionCaptureConfig:
    """Metrics for contextual extraction flows."""
    max_items_per_session: int = 5
    min_turns: int = 3
    types: list[str] = field(default_factory=lambda: ["Decision", "ProjectSnapshot", "Insight", "PersonMemory", "ActionItem"])

@dataclass
class DailyCaptureConfig:
    """Boundaries for chronologic diary capture modes."""
    enabled: bool = True
    max_messages: int = 10
    max_chars: int = 2000

@dataclass
class QuickCaptureConfig:
    """Short-form memory input threshold settings."""
    enabled: bool = True
    min_chars: int = 5
    long_text_threshold: int = 500

@dataclass
class CaptureConfig:
    """General extraction capability configuration map."""
    enabled: bool = True
    extraction: ExtractionCaptureConfig = field(default_factory=ExtractionCaptureConfig)
    daily: DailyCaptureConfig = field(default_factory=DailyCaptureConfig)
    quick_capture: QuickCaptureConfig = field(default_factory=QuickCaptureConfig)

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
    provider: str = "ollama"
    model: str = "bge-m3"
    url: str = "http://localhost:11434"
    dimensions: int = 1024
    timeout_seconds: int = 120
    connect_timeout_seconds: int = 10

@dataclass
class ResearchEmbeddingConfig:
    """Configuration for secondary embedding engine processing constraints."""
    enabled: bool = False
    provider: str = "gemini"
    model: str = "gemini-embedding-2-preview"
    dimensions: int = 768
    timeout_seconds: int = 30

@dataclass
class EmbeddingsConfig:
    """Mapping schema bridging multiple embedding targets."""
    primary: PrimaryEmbeddingConfig = field(default_factory=PrimaryEmbeddingConfig)
    research: ResearchEmbeddingConfig = field(default_factory=ResearchEmbeddingConfig)

@dataclass
class AutoSummaryConfig:
    """Inference automation definitions for semantic summarization."""
    enabled: bool = True
    model: str = "qwen2.5:14b-instruct"
    max_chars: int = 120
    min_content_chars: int = 200
    ollama_url: str | None = None

@dataclass
class SearchConfig:
    """Matching index score cutoffs thresholds layouts."""
    mcp_threshold: float = 0.4
    api_threshold: float = 0.6
    default_limit: int = 10
    exclude_status: list[str] = field(default_factory=lambda: ["archived"])
    hybrid_weight: float = 0.5
    hybrid_enabled: bool = True
    dedup_score_gap: float = 0.2
    daily_penalty: float = 0.3  # Multiplier for daily/ files (0.3 = 30% of original score)

@dataclass
class NightlyConfig:
    """Lightweight daily update configurations."""
    enabled: bool = True
    lookback_days: int = 1
    allowed_ops: list[str] = field(default_factory=lambda: ["UPDATE", "SUPERSEDE"])

@dataclass
class WriteTimeConfig:
    """Tier 2a: write-time contradiction check on palinode_save.

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
class ConsolidationConfig:
    """Interval LLM job configuration settings logic."""
    enabled: bool = True
    schedule: str = "0 3 * * 0"  # Sunday 3am UTC
    lookback_days: int = 7
    max_files: int = 30
    # LLM for consolidation tasks (OpenAI-compatible API)
    llm_url: str = "http://localhost:8000"
    llm_model: str = "/model"
    llm_fallbacks: list[dict] = field(default_factory=list)
    llm_temperature: float = 0.3
    llm_max_tokens: int = 2000
    nightly: NightlyConfig = field(default_factory=NightlyConfig)
    write_time: WriteTimeConfig = field(default_factory=WriteTimeConfig)
    keyword_map: dict[str, list[str]] | None = None

@dataclass
class DecayConfig:
    """Algorithm constraints matching temporal decay curves settings."""
    enabled: bool = False
    tau_critical: int = 180
    tau_decisions: int = 60
    tau_insights: int = 90
    tau_general: int = 30
    tau_status: int = 7
    tau_ephemeral: int = 1

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
class SecurityConfig:
    """Configurations mappings for scanning logic outputs endpoints schemas."""
    scrub_patterns_file: str = "specs/scrub-patterns.yaml"
    exclude_paths: list[str] = field(default_factory=lambda: [".secrets", "credentials", "passwords"])

@dataclass
class GitConfig:
    """Git logic auto execution formats limits inputs metrics."""
    auto_commit: bool = True
    auto_push: bool = False
    commit_prefix: str = "palinode"

@dataclass
class AuditConfig:
    """MCP tool call audit logging for compliance and debugging."""
    enabled: bool = True
    log_path: str = ".audit/mcp-calls.jsonl"

@dataclass
class LoggingConfig:
    """Log formatting and target directories constraints formats."""
    level: str = "INFO"
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
    - Use `layer_hint: status` or `layer_hint: identity` in file frontmatter to
      override the heuristic for specific files
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
class ScopeConfig:
    """Layer 1: scope chain for multi-harness, multi-agent, team memory.

    Scopes form an entity-ref hierarchy: org → member → project → harness → agent → session.
    Memories inherit DOWN the chain by default. A session's scope is resolved from
    env vars and config.

    Layer 1 scope (this slice): resolution only — produces a ScopeChain from
    config + env. Later slices wire the chain into store search, the
    /context/prime endpoint, and frontmatter `scope` field parsing.

    Env vars:
      PALINODE_ORG      → scope.org
      PALINODE_MEMBER   → scope.member
      PALINODE_HARNESS  → scope.harness  (MCP client auto-detection is Layer 2+)
      PALINODE_AGENT    → scope.agent    (multi-agent orchestration only)

    prime_mode:
      "classic" — inject all core files regardless of scope (legacy, default
                  during Layer 1 rollout for backwards compatibility).
      "scoped"  — filter core files by the session's scope chain. Flip the
                  default to "scoped" in a follow-up once Slices 2-3 land.
    """
    enabled: bool = False
    org: str | None = None
    member: str | None = None
    harness: str | None = None
    agent: str | None = None
    prime_mode: str = "classic"


@dataclass
class CompactionConfig:
    """Operations controls algorithms parameters logic models layouts mapping endpoints."""
    # Which operations are allowed
    allowed_ops: list[str] = field(default_factory=lambda:
        ["KEEP", "UPDATE", "MERGE", "SUPERSEDE", "ARCHIVE", "RETRACT"])
    # How aggressive: conservative = mostly KEEP, aggressive = more MERGE/ARCHIVE
    aggressiveness: str = "moderate"  # "conservative" | "moderate" | "aggressive"
    # Layer split heuristics
    layer_split: LayerSplitConfig = field(default_factory=LayerSplitConfig)

@dataclass
class Config:
    """Global configuration model mapping all schema structures format maps formats outputs."""
    memory_dir: str = "~/palinode"
    db_path: str = ".palinode.db"
    recall: RecallConfig = field(default_factory=RecallConfig)
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    ingestion: IngestionConfig = field(default_factory=IngestionConfig)
    embeddings: EmbeddingsConfig = field(default_factory=EmbeddingsConfig)
    auto_summary: AutoSummaryConfig = field(default_factory=AutoSummaryConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    consolidation: ConsolidationConfig = field(default_factory=ConsolidationConfig)
    compaction: CompactionConfig = field(default_factory=CompactionConfig)
    context: ContextConfig = field(default_factory=ContextConfig)
    scope: ScopeConfig = field(default_factory=ScopeConfig)
    decay: DecayConfig = field(default_factory=DecayConfig)
    services: ServicesConfig = field(default_factory=ServicesConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    git: GitConfig = field(default_factory=GitConfig)
    audit: AuditConfig = field(default_factory=AuditConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    
    @property
    def palinode_dir(self) -> str:
        return self.memory_dir

    def __post_init__(self):
        # Support ~ expansion in specific paths
        self.memory_dir = _expand_path(self.memory_dir)
        # Handle db_path absolute or relative
        if not os.path.isabs(self.db_path):
            self.db_path = os.path.join(self.memory_dir, self.db_path)


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
                with open(cpath, 'r') as f:
                    file_conf = yaml.safe_load(f) or {}
                    _deep_merge(raw_config, file_conf)
                loaded_path = cpath
            except Exception as e:
                print(f"Warning: Failed to load config from {cpath}: {e}")

    # Initialize dataclass with Pydantic validation
    try:
        adapter = TypeAdapter(Config)
        cfg = adapter.validate_python(raw_config)
    except ValidationError as e:
        print(f"Failed to validate configuration:\n{e}")
        raise

    # 4. Environment variable overrides
    if "PALINODE_DIR" in os.environ:
        cfg.memory_dir = _expand_path(os.environ["PALINODE_DIR"])
        if not os.path.isabs(cfg.db_path):
            cfg.db_path = os.path.join(cfg.memory_dir, os.path.basename(cfg.db_path))
    if "OLLAMA_URL" in os.environ:
        cfg.embeddings.primary.url = os.environ["OLLAMA_URL"]
    if "EMBEDDING_MODEL" in os.environ:
        cfg.embeddings.primary.model = os.environ["EMBEDDING_MODEL"]
    if "GEMINI_API_KEY" in os.environ:
        cfg.embeddings.research.enabled = True
    if "PALINODE_API_HOST" in os.environ:
        cfg.services.api.host = os.environ["PALINODE_API_HOST"]
    if "PALINODE_API_PORT" in os.environ:
        try:
            cfg.services.api.port = int(os.environ["PALINODE_API_PORT"])
        except ValueError:
            pass
    if "PALINODE_ORG" in os.environ:
        cfg.scope.org = os.environ["PALINODE_ORG"]
    if "PALINODE_MEMBER" in os.environ:
        cfg.scope.member = os.environ["PALINODE_MEMBER"]
    if "PALINODE_HARNESS" in os.environ:
        cfg.scope.harness = os.environ["PALINODE_HARNESS"]
    if "PALINODE_AGENT" in os.environ:
        cfg.scope.agent = os.environ["PALINODE_AGENT"]

    # Print summary string
    try:
        num_files = len(glob.glob(os.path.join(cfg.memory_dir, "**/*.md"), recursive=True))
    except (OSError, ValueError):
        num_files = 0

    print(f"Palinode config: {loaded_path or 'defaults'} "
          f"({num_files} files, {cfg.embeddings.primary.model} @ {cfg.embeddings.primary.url})")
    
    return cfg


# Singleton config instance
config = load_config()
