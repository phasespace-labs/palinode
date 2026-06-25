"""
Cross-surface parity registry — the canonical-names contract for ADR-010.

Every memory operation that should appear on more than one surface
(CLI, MCP, REST API, OpenClaw plugin) is enumerated here with one
canonical name per parameter and one canonical shape (type + required
flag).  ``tests/test_surface_parity.py`` walks this registry and asserts
each surface conforms.

When you add a parameter to one surface, add it here first, then
mirror to the others.  When the four surfaces drift, record the drift
in ``known_drift`` with the GitHub issue number — the test xfails the
drift entry until the issue closes.

Admin-only operations (reindex, migrations, doctor, etc.) are explicitly
exempt from parity by listing them in ``ADMIN_EXEMPT_OPERATIONS``.  The
contract is "all memory operations are equivalent across surfaces, by
design"; it is *not* "all operations appear everywhere".

See ``ADR-010-cross-surface-parity-contract.md`` and ``docs/PARITY.md``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

ParamType = Literal["string", "boolean", "array", "integer", "number", "object"]
Surface = Literal["cli", "mcp", "api", "plugin"]


# ─────────────────────────────────────────────────────────────────────────────
# Data model
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CanonicalParam:
    """One parameter, named and shaped as it should appear on every surface."""

    name: str
    type: ParamType
    required: bool = False
    #: If the default is shared across surfaces, this is the attribute name in
    #: ``palinode.core.defaults``.  ``None`` means "no default" or "surface-
    #: specific default that we accept by design".
    default_key: str | None = None
    #: Closed set of allowed values, if any.  Surfaces that expose an enum
    #: must use this exact tuple (order-insensitive).
    enum: tuple[str, ...] | None = None
    notes: str = ""


@dataclass(frozen=True)
class Operation:
    """A memory operation with its canonical params and per-surface mapping."""

    name: str
    canonical_params: tuple[CanonicalParam, ...]
    cli_command: str | None = None
    mcp_tool: str | None = None
    api_endpoint: tuple[str, str] | None = None  # (METHOD, path)
    plugin_tool: str | None = None
    #: Surfaces in this set are *not* required to expose the operation.
    #: Useful when something is intentionally CLI-only (admin) or
    #: API-only (internal observability) — see ``ADMIN_EXEMPT_OPERATIONS``
    #: for the global admin carve-out.
    exempt_surfaces: frozenset[Surface] = field(default_factory=frozenset)
    #: Known drift, keyed by ``(surface, param_name)``.  Value is the GitHub
    #: issue number tracking the fix.  The parity test reports these as xfail
    #: with the issue ref — once the issue closes and the surface is fixed,
    #: remove the entry and the test enforces.
    known_drift: dict[tuple[Surface, str], int] = field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────────────
# Admin carve-out
# ─────────────────────────────────────────────────────────────────────────────


#: Operations that are intentionally NOT subject to cross-surface parity.
#: They appear on whichever surfaces make operational sense (typically CLI +
#: API, sometimes only one).  Adding parity for these requires a new ADR.
ADMIN_EXEMPT_OPERATIONS: frozenset[str] = frozenset(
    {
        # Full-database operations (CLI + API only)
        "reindex",
        "rebuild-fts",
        "split-layers",
        "bootstrap-fact-ids",
        # One-off importers (CLI + API only)
        "migrate-openclaw",
        "migrate-mem0",
        # Local / operational (CLI only)
        "doctor",
        "start",
        "stop",
        "config",
        "banner",
        # Observability internals (API only)
        "health",
        "git-stats",
        "generate-summaries",
    }
)


# ─────────────────────────────────────────────────────────────────────────────
# Canonical category + type sets
# ─────────────────────────────────────────────────────────────────────────────


#: The canonical ``category`` enum.  Matches the memory-directory names
#: (plural) — that is the value the ``chunks.category`` column stores
#: (``palinode/api/server.py:660-668`` and the watcher's directory-basename
#: derivation).  Surfaces that expose a ``category`` filter MUST use
#: this exact tuple — ADR-010, finding #161.
CATEGORIES: tuple[str, ...] = (
    "people",
    "projects",
    "decisions",
    "insights",
    "research",
)

#: The canonical memory ``type`` enum (used by save).  Lives here so
#: the API can validate ``SaveRequest.type`` server-side instead of
#: relying on per-surface enum lists.  ADR-010, finding #166.
MEMORY_TYPES: tuple[str, ...] = (
    "PersonMemory",
    "Decision",
    "ProjectSnapshot",
    "Insight",
    "ResearchRef",
    "ActionItem",
)

#: The canonical prompt-task enum.  Single source replacing the duplicate
#: ``"enum"`` keys at ``palinode/mcp.py:624-625``.  ADR-010, finding #162.
PROMPT_TASKS: tuple[str, ...] = (
    "compaction",
    "extraction",
    "update",
    "classification",
    "nightly-consolidation",
)


# ─────────────────────────────────────────────────────────────────────────────
# The registry
# ─────────────────────────────────────────────────────────────────────────────


REGISTRY: tuple[Operation, ...] = (
    # ── status ──────────────────────────────────────────────────────────────
    Operation(
        name="status",
        canonical_params=(),
        cli_command="status",
        mcp_tool="palinode_status",
        api_endpoint=("GET", "/status"),
    ),
    # ── list ────────────────────────────────────────────────────────────────
    Operation(
        name="list",
        canonical_params=(
            CanonicalParam(name="category", type="string", enum=CATEGORIES),
            CanonicalParam(name="core_only", type="boolean"),
        ),
        cli_command="list",
        mcp_tool="palinode_list",
        api_endpoint=("GET", "/list"),
        exempt_surfaces=frozenset({"plugin"}),
    ),
    # ── read ────────────────────────────────────────────────────────────────
    Operation(
        name="read",
        canonical_params=(
            CanonicalParam(name="file_path", type="string", required=True),
            CanonicalParam(name="meta", type="boolean"),
        ),
        cli_command="read",
        mcp_tool="palinode_read",
        api_endpoint=("GET", "/read"),
        exempt_surfaces=frozenset({"plugin"}),
        known_drift={},
    ),
    # ── search ──────────────────────────────────────────────────────────────
    Operation(
        name="search",
        canonical_params=(
            CanonicalParam(name="query", type="string", required=True),
            CanonicalParam(
                name="limit", type="integer", default_key="SEARCH_LIMIT_DEFAULT"
            ),
            CanonicalParam(name="category", type="string", enum=CATEGORIES),
            CanonicalParam(
                name="threshold",
                type="number",
                default_key="SEARCH_THRESHOLD_DEFAULT",
            ),
            CanonicalParam(name="since_days", type="integer"),
            CanonicalParam(name="types", type="array"),
            CanonicalParam(name="min_priority", type="integer"),
            CanonicalParam(name="date_after", type="string"),
            CanonicalParam(name="date_before", type="string"),
            CanonicalParam(name="include_daily", type="boolean"),
            # ADR-015 §5 (#480): telemetry-exclusion override; default false so
            # monitoring writes don't pollute recall. First-class on all surfaces.
            CanonicalParam(name="include_telemetry", type="boolean"),
        ),
        cli_command="search",
        mcp_tool="palinode_search",
        api_endpoint=("POST", "/search"),
        plugin_tool="palinode_search",
        known_drift={},
    ),
    # ── save ────────────────────────────────────────────────────────────────
    # NOTE on ``ps``: deliberately *not* a canonical parameter.  CLI ``--ps``
    # and MCP ``ps`` are surface sugar that resolves to ``type=ProjectSnapshot``
    # locally before hitting the API.  Documented in ``docs/PARITY.md``;
    # surfaces are free to add the shortcut without parity overhead.  The
    # plugin currently lacks it (#166 expansion), but adding it is a plugin
    # courtesy, not a parity obligation.
    Operation(
        name="save",
        canonical_params=(
            CanonicalParam(name="content", type="string", required=True),
            CanonicalParam(name="type", type="string", enum=MEMORY_TYPES),
            CanonicalParam(name="entities", type="array"),
            CanonicalParam(name="project", type="string"),
            CanonicalParam(name="metadata", type="object"),
            CanonicalParam(name="confidence", type="number"),
            CanonicalParam(name="priority", type="integer"),
            CanonicalParam(name="external_refs", type="object"),
            CanonicalParam(name="title", type="string"),
            CanonicalParam(name="slug", type="string"),
            CanonicalParam(name="core", type="boolean"),
            CanonicalParam(name="source", type="string"),
            # ADR-015 §5 (#480): write-semantics axis. "append" (default) is
            # episodic; "replace" marks a living/current-state document. First-class
            # on all surfaces so callers don't need to tunnel it through metadata.
            CanonicalParam(
                name="update_policy",
                type="string",
                enum=("append", "replace"),
            ),
            # #459: source-citation anchors. A list of {ref, quote, quote_hash}
            # dicts; the quote_hash is computed/verified on save. First-class on
            # all surfaces so callers don't tunnel citations through metadata.
            # The CLI surfaces this as the repeatable ``--cite REF::QUOTE`` flag
            # (dest ``sources``); MCP/API/plugin take the structured list.
            CanonicalParam(name="sources", type="array"),
        ),
        cli_command="save",
        mcp_tool="palinode_save",
        api_endpoint=("POST", "/save"),
        plugin_tool="palinode_save",
        known_drift={},
    ),
    # ── consolidate ─────────────────────────────────────────────────────────
    Operation(
        name="consolidate",
        canonical_params=(
            CanonicalParam(name="dry_run", type="boolean"),
            CanonicalParam(name="nightly", type="boolean"),
        ),
        cli_command="consolidate",
        mcp_tool="palinode_consolidate",
        api_endpoint=("POST", "/consolidate"),
        exempt_surfaces=frozenset({"plugin"}),
        known_drift={},
    ),
    # ── archive-expired (ADR-015 §2.3 TTL sweep, #482) ──────────────────────
    Operation(
        name="archive_expired",
        canonical_params=(
            CanonicalParam(name="dry_run", type="boolean"),
        ),
        cli_command="archive-expired",
        mcp_tool="palinode_archive_expired",
        api_endpoint=("POST", "/archive-expired"),
        exempt_surfaces=frozenset({"plugin"}),
        known_drift={},
    ),
    # ── trigger (create) ────────────────────────────────────────────────────
    # Trigger is multi-action.  We model the most cross-surface-relevant one,
    # ``create``, and let the others (list, delete) be tested via simpler
    # presence-only checks (or as separate Operation entries when they have
    # parameters worth pinning).
    Operation(
        name="trigger.create",
        canonical_params=(
            CanonicalParam(name="description", type="string", required=True),
            CanonicalParam(name="memory_file", type="string", required=True),
            CanonicalParam(name="trigger_id", type="string"),
            CanonicalParam(
                name="threshold",
                type="number",
                default_key="TRIGGER_THRESHOLD_DEFAULT",
            ),
            CanonicalParam(
                name="cooldown_hours",
                type="integer",
                default_key="TRIGGER_COOLDOWN_HOURS_DEFAULT",
            ),
        ),
        cli_command="trigger add",
        mcp_tool="palinode_trigger",
        api_endpoint=("POST", "/triggers"),
        exempt_surfaces=frozenset({"plugin"}),
        known_drift={},
    ),
    # ── rollback ────────────────────────────────────────────────────────────
    Operation(
        name="rollback",
        canonical_params=(
            CanonicalParam(name="file_path", type="string", required=True),
            CanonicalParam(name="commit", type="string"),
            CanonicalParam(name="dry_run", type="boolean"),
        ),
        cli_command="rollback",
        mcp_tool="palinode_rollback",
        api_endpoint=("POST", "/rollback"),
        exempt_surfaces=frozenset({"plugin"}),
        known_drift={},
    ),
    # ── blame ───────────────────────────────────────────────────────────────
    Operation(
        name="blame",
        canonical_params=(
            CanonicalParam(name="file_path", type="string", required=True),
            CanonicalParam(name="search", type="string"),
        ),
        cli_command="blame",
        mcp_tool="palinode_blame",
        api_endpoint=("GET", "/blame/{file_path:path}"),
        exempt_surfaces=frozenset({"plugin"}),
        known_drift={},
    ),
    # ── cluster_neighbors (#235) ─────────────────────────────────────────────
    Operation(
        name="cluster_neighbors",
        canonical_params=(
            CanonicalParam(name="file_path", type="string", required=True),
            CanonicalParam(name="min_similarity", type="number"),
            CanonicalParam(name="top_k", type="integer"),
        ),
        cli_command="cluster-neighbors",
        mcp_tool="palinode_cluster_neighbors",
        api_endpoint=("POST", "/cluster-neighbors"),
        exempt_surfaces=frozenset({"plugin"}),
        known_drift={},
    ),
    # ── topic_coverage (#235) ────────────────────────────────────────────────
    Operation(
        name="topic_coverage",
        canonical_params=(
            CanonicalParam(name="query", type="string", required=True),
            CanonicalParam(name="min_similarity", type="number"),
        ),
        cli_command="topic-coverage",
        mcp_tool="palinode_topic_coverage",
        api_endpoint=("POST", "/topic-coverage"),
        exempt_surfaces=frozenset({"plugin"}),
        known_drift={},
    ),
    # ── depends (#97) ────────────────────────────────────────────────────────
    # The `unblocked` mode is exposed as a separate REST endpoint
    # (GET /depends/_unblocked) rather than a query param on
    # GET /depends/{slug}, so it does not appear in the API endpoint's
    # function signature.  Recorded as known drift to keep the parity test
    # from failing; the endpoint exists but under a different URL.
    Operation(
        name="depends",
        canonical_params=(
            CanonicalParam(name="slug", type="string"),
            CanonicalParam(name="unblocked", type="boolean"),
        ),
        cli_command="depends",
        mcp_tool="palinode_depends",
        api_endpoint=("GET", "/depends/{slug:path}"),
        plugin_tool="palinode_depends",
        known_drift={
            ("api", "unblocked"): 97,
        },
    ),
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def by_name(op_name: str) -> Operation:
    """Look up an operation by name.  Raises ``KeyError`` if missing."""
    for op in REGISTRY:
        if op.name == op_name:
            return op
    raise KeyError(op_name)


def required_surfaces(op: Operation) -> frozenset[Surface]:
    """Return the surfaces this operation must appear on (i.e. not exempt)."""
    all_surfaces: frozenset[Surface] = frozenset({"cli", "mcp", "api", "plugin"})
    return all_surfaces - op.exempt_surfaces


__all__ = [
    "ADMIN_EXEMPT_OPERATIONS",
    "CATEGORIES",
    "CanonicalParam",
    "MEMORY_TYPES",
    "Operation",
    "PROMPT_TASKS",
    "ParamType",
    "REGISTRY",
    "Surface",
    "by_name",
    "required_surfaces",
]
