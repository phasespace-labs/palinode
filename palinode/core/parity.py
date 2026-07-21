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
#: this exact tuple — ADR-010, finding.
CATEGORIES: tuple[str, ...] = (
    "people",
    "projects",
    "decisions",
    "insights",
    "research",
)

#: The canonical memory ``type`` enum (used by save).  Lives here so
#: the API can validate ``SaveRequest.type`` server-side instead of
#: relying on per-surface enum lists. ADR-010, finding.
MEMORY_TYPES: tuple[str, ...] = (
    "PersonMemory",
    "Decision",
    "ProjectSnapshot",
    "Insight",
    "ResearchRef",
    "ActionItem",
)

#: The canonical prompt-task enum.  Single source replacing the duplicate
#: ``"enum"`` keys at ``palinode/mcp.py:624-625``. ADR-010, finding.
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
            # ADR-015 §5: telemetry-exclusion override; default false so
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
    # plugin currently lacks it (expansion), but adding it is a plugin
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
            # ADR-015 §5: write-semantics axis. "append" (default) is
            # episodic; "replace" marks a living/current-state document. First-class
            # on all surfaces so callers don't need to tunnel it through metadata.
            CanonicalParam(
                name="update_policy",
                type="string",
                enum=("append", "replace"),
            ),
            # source-citation anchors. A list of {ref, quote, quote_hash}
            # dicts; the quote_hash is computed/verified on save. First-class on
            # all surfaces so callers don't tunnel citations through metadata.
            # The CLI surfaces this as the repeatable ``--cite REF::QUOTE`` flag
            # (dest ``sources``); MCP/API/plugin take the structured list.
            CanonicalParam(name="sources", type="array"),
            # (G4): typed relationship links. ``contradicts`` records a
            # conflict with no winner picked; ``backed_by`` records an
            # evidence/support edge. First-class plaintext lists on all surfaces
            # so callers don't tunnel them through metadata. CLI surfaces them as
            # the repeatable ``--contradicts``/``--backed-by`` flags.
            CanonicalParam(name="contradicts", type="array"),
            CanonicalParam(name="backed_by", type="array"),
            # claim-level source anchors. A list of {claim_id?, text,
            # source_id, span:{quote, quote_hash}, anchor_id?} dicts binding a
            # claim inside the memory to the source span that justifies it;
            # claim_id + quote_hash are derived/verified on save. First-class
            # on all surfaces so callers don't tunnel bindings through
            # metadata. The CLI surfaces this as the repeatable
            # ``--claim TEXT::REF::QUOTE`` flag (dest ``claims``); MCP/API/
            # plugin take the structured list.
            CanonicalParam(name="claims", type="array"),
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
    # ── archive-expired (ADR-015 §2.3 TTL sweep) ──────────────────────
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
    # ── context_prime (ADR-012 Layer 4) ─────────────────────────────────────
    # Session-start context digest. cwd resolves the project scope (ADR-008
    # resolution); project overrides. REST additionally accepts session_id
    # (SessionStart-hook compat, reserved) — a superset, not drift.
    Operation(
        name="context_prime",
        canonical_params=(
            CanonicalParam(name="cwd", type="string"),
            CanonicalParam(name="project", type="string"),
        ),
        cli_command="prime",
        mcp_tool="palinode_session_init",
        api_endpoint=("POST", "/context/prime"),
        exempt_surfaces=frozenset({"plugin"}),
        known_drift={},
    ),
    # ── blame ───────────────────────────────────────────────────────────────
    Operation(
        name="blame",
        canonical_params=(
            CanonicalParam(name="file_path", type="string", required=True),
            CanonicalParam(name="search", type="string"),
            # claim resolution mode: also resolve the file's claim-level
            # source anchors to their cited spans with live integrity status,
            # so blame answers "which source span justifies this claim".
            CanonicalParam(name="claims", type="boolean"),
        ),
        cli_command="blame",
        mcp_tool="palinode_blame",
        api_endpoint=("GET", "/blame/{file_path:path}"),
        exempt_surfaces=frozenset({"plugin"}),
        known_drift={},
    ),
    # ── trace (C1 provenance composition) ────────────────────────────────────
    # Composes the provenance primitives (source citations, blame/history,
    # supersession trail, typed links, retrieval log) into one lineage view for
    # a file. Read-only. Plugin-exempt like its git-provenance siblings
    # (blame/rollback/history).
    Operation(
        name="trace",
        canonical_params=(
            CanonicalParam(name="file_path", type="string", required=True),
        ),
        cli_command="trace",
        mcp_tool="palinode_trace",
        api_endpoint=("GET", "/trace/{file_path:path}"),
        exempt_surfaces=frozenset({"plugin"}),
        known_drift={},
    ),
    # ── cluster_neighbors ─────────────────────────────────────────────
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
    # ── topic_coverage ────────────────────────────────────────────────
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
    # ── review ────────────────────────────────────────────────────────
    # Advisory project-memory review. Composes the deterministic lint signals
    # scoped to a project and proposes corrective ops (read-only). Plugin-exempt
    # like the other quality/maintenance ops (lint/topic_coverage/consolidate).
    Operation(
        name="review",
        canonical_params=(
            CanonicalParam(name="project", type="string"),
        ),
        cli_command="review",
        mcp_tool="palinode_review",
        api_endpoint=("POST", "/review"),
        exempt_surfaces=frozenset({"plugin"}),
        known_drift={},
    ),
    # ── depends ────────────────────────────────────────────────────────
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
# Inventory accounting — the surface→registry direction
# ─────────────────────────────────────────────────────────────────────────────
#
# ``REGISTRY`` drives the param-level parity test (registry→surface).  That
# direction cannot catch a *new* capability shipped on a surface but never
# registered: the test only walks operations it already knows about.  The
# inventory guard closes the reverse direction — it enumerates the live
# capabilities of every surface and asserts each one is accounted for by
# exactly one of:
#
#   1. ``REGISTRY``                — a parity-bound memory operation (mapped via
#      that operation's ``mcp_tool`` / ``api_endpoint`` / ``cli_command``);
#   2. ``INVENTORY_INFRA``         — framework / admin / observability surface
#      that is *not* a memory operation (docs UI, OpenAPI schema, the HTML
#      inspector, liveness probes, DB-maintenance and importer endpoints).
#      The surface-identifier form of ``ADMIN_EXEMPT_OPERATIONS`` plus the
#      framework routes;
#   3. ``INVENTORY_BACKLOG``       — a *memory-semantic* operation that already
#      ships on the surface but has not yet been promoted into ``REGISTRY``
#      with canonical params.  These are the ADR-010 implementation backlog
#      (issue); they are acknowledged, not silently ignored.
#
# A live capability that is in none of the three buckets fails the guard:
# that is a brand-new operation that skipped the contract.  A bucket entry
# that is no longer live also fails: the capability was renamed/removed and
# the accounting is stale.  Both mirror the ``known_drift`` hygiene rule.
#
# Identifier form per surface:
#   - mcp: the tool name           (e.g. ``"palinode_search"``)
#   - api: ``"METHOD /path"``      (e.g. ``"POST /search"``)
#   - cli: the command path        (e.g. ``"trigger add"``)


#: Framework / admin / observability surface capabilities that are *not*
#: memory operations and are exempt from the inventory guard by nature.
#: This is ``ADMIN_EXEMPT_OPERATIONS`` expressed in per-surface identifier
#: form, plus the framework-provided routes (Swagger/Redoc/OpenAPI, the HTML
#: inspector UI under ``/ui``, liveness probes).
INVENTORY_INFRA: dict[Surface, frozenset[str]] = {
    "mcp": frozenset(
        {
            "palinode_doctor",  # diagnostics (admin: doctor)
            "palinode_doctor_deep",  # deep diagnostics (admin: doctor)
        }
    ),
    "api": frozenset(
        {
            # FastAPI framework routes
            "GET /docs",
            "GET /docs/oauth2-redirect",
            "GET /openapi.json",
            "GET /redoc",
            # HTML inspector UI
            "GET /ui",
            "GET /ui/",
            "GET /ui/compaction",
            "GET /ui/diffs",
            "GET /ui/history/{file_path:path}",
            "GET /ui/memory",
            "GET /ui/memory/{file_path:path}",
            "GET /ui/quality",
            # Observability internals (ADMIN_EXEMPT: health, git-stats,
            # generate-summaries) + diagnostics
            "GET /doctor",
            "GET /git-stats",
            "GET /health",
            "GET /health/auto-summary",
            "GET /health/watcher",
            "POST /generate-summaries",
            # Full-database operations (ADMIN_EXEMPT)
            "POST /reindex",
            "POST /rebuild-fts",
            "POST /split-layers",
            "POST /bootstrap-fact-ids",
            # One-off importers (ADMIN_EXEMPT)
            "POST /migrate/mem0",
            "POST /migrate/openclaw",
        }
    ),
    "cli": frozenset(
        {
            # Local / operational (ADMIN_EXEMPT)
            "banner",
            "config edit",
            "config view",
            "doctor",
            "start",
            "stop",
            # Full-database operations (ADMIN_EXEMPT)
            "bootstrap-ids",
            "rebuild-fts",
            "reindex",
            "split-layers",
            # One-off importers (ADMIN_EXEMPT) + local sync/scaffolding helpers
            "import from-vault",
            "init",
            "mcp-config",
            "mcp-smoke",
            "migrate openclaw",
            "migrate-mem0",
            "obsidian-sync",
            "retrieval-stats",
            "worktree-reconcile",
        }
    ),
}


#: Memory-semantic operations that already ship on a surface but have **not**
#: yet been promoted into ``REGISTRY`` with canonical params.  This is the
#: ADR-010 implementation backlog — each entry maps to the GitHub issue that
#: tracks adding it to the registry.  The guard acknowledges these so the
#: suite stays green, but FAILS the moment a *new* capability appears that is
#: neither registered, infra, nor backlog.  Promoting one of these into
#: ``REGISTRY`` means removing its entry here (the guard fails on the overlap,
#: telling you the move is done).
INVENTORY_BACKLOG: dict[Surface, dict[str, int]] = {
    "mcp": {
        "palinode_dedup_suggest": 170,
        "palinode_diff": 170,
        "palinode_entities": 170,
        "palinode_history": 170,
        "palinode_ingest": 170,
        "palinode_lint": 170,
        "palinode_orphan_repair": 170,
        "palinode_prompt": 170,
        "palinode_push": 170,
        "palinode_session_end": 170,
        "palinode_timeline": 170,
    },
    "api": {
        "DELETE /triggers/{trigger_id}": 170,
        "GET /triggers": 170,
        "GET /depends/_unblocked": 97,
        "GET /diff": 170,
        "GET /entities": 170,
        "GET /entities/{entity_ref:path}": 170,
        "GET /history/{file_path:path}": 170,
        "GET /prompts": 170,
        "GET /prompts/{name}": 170,
        "GET /timeline/{file_path:path}": 170,
        "POST /check-triggers": 170,
        "POST /dedup-suggest": 170,
        "POST /ingest": 170,
        "POST /ingest-url": 170,
        "POST /lint": 170,
        "POST /orphan-repair": 170,
        "POST /prompts/{name}/activate": 170,
        "POST /push": 170,
        "POST /search-associative": 170,
        "POST /session-end": 170,
    },
    "cli": {
        "dedup-suggest": 170,
        "diff": 170,
        "entities": 170,
        "history": 170,
        "ingest": 170,
        "lint": 170,
        "orphan-repair": 170,
        "prompt activate": 170,
        "prompt list": 170,
        "prompt show": 170,
        "push": 170,
        "session-end": 170,
        "timeline": 170,
        "trigger list": 170,
        "trigger remove": 170,
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def registered_capabilities(surface: Surface) -> frozenset[str]:
    """Return the surface-identifier form of every ``REGISTRY`` operation
    that maps to ``surface``.

    The identifier matches the live-introspection form used by the inventory
    guard: the MCP tool name, ``"METHOD /path"`` for the API, the Click
    command path for the CLI.
    """
    ids: set[str] = set()
    for op in REGISTRY:
        if surface == "mcp" and op.mcp_tool is not None:
            ids.add(op.mcp_tool)
        elif surface == "api" and op.api_endpoint is not None:
            method, path = op.api_endpoint
            ids.add(f"{method} {path}")
        elif surface == "cli" and op.cli_command is not None:
            ids.add(op.cli_command)
    return frozenset(ids)


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
    "INVENTORY_BACKLOG",
    "INVENTORY_INFRA",
    "MEMORY_TYPES",
    "Operation",
    "PROMPT_TASKS",
    "ParamType",
    "REGISTRY",
    "Surface",
    "by_name",
    "registered_capabilities",
    "required_surfaces",
]
