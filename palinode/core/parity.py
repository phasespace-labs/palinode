"""
Cross-surface parity registry — the canonical-names contract for ADR-010.

Every memory operation that should appear on more than one FIRST-PARTY
surface (CLI, MCP, REST API) is enumerated here with one canonical name per
parameter and one canonical shape (type + required flag).
``tests/test_surface_parity.py`` walks this registry and asserts each
surface conforms.

**Plugins are different (ADR-019).** A plugin is a delivery adapter over the
REST API, not a capability surface, so the ``plugin`` surface is OPT-IN per
operation: an operation joins the plugin's parity obligations only by
naming a ``plugin_tool``, and non-implementation is the expected case, not
drift. (Before ADR-019 the registry paid ``exempt_surfaces={"plugin"}`` on
13 operations for what was simply the default reality.) ``plugin`` denotes
the *plugin contract* — what any Palinode plugin must honor for the
operations it does expose — not one implementation; additional plugins add
no parity obligations.

When you add a parameter to one surface, add it here first, then
mirror to the others.  When surfaces drift, record the drift
in ``known_drift`` with the GitHub issue number — the test xfails the
drift entry until the issue closes.

Admin-only operations (reindex, migrations, doctor, etc.) are explicitly
exempt from parity by listing them in ``ADMIN_EXEMPT_OPERATIONS``.  The
contract is "all memory operations are equivalent across surfaces, by
design"; it is *not* "all operations appear everywhere".

See ADR-010 (the cross-surface parity contract), ADR-019 (plugins are
delivery adapters), and ``docs/PARITY.md``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# ── This module imports NOTHING outside the standard library. Keep it that way.
#
# `plugin/test/parity.test.ts` regenerates `plugin/parity-registry.json` from
# this file at `pretest`, running a bare `python` in a Node-only CI job with no
# project dependencies installed. Any third-party import here — directly, or
# transitively via another palinode module — breaks the TypeScript side of the
# parity contract with a `ModuleNotFoundError` that has nothing to do with
# parity. That is why the canonical enums below live *here* and are re-exported
# by their consumers, rather than being imported from a module that parses
# frontmatter. `tests/test_parity_is_dependency_free.py` enforces it.

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


@dataclass(frozen=True)
class InventoryBacklogEntry:
    """One unregistered capability, including alternate surface names."""

    issue: int
    aliases: tuple[str, ...] = ()


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
        # One-off importers / migrations (CLI + API only; the frontmatter
        # backfill is CLI-only by design — a whole-store mutation should not be
        # remotely triggerable over HTTP)
        "migrate-openclaw",
        "migrate-frontmatter",
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

#: The canonical write-semantics enum (ADR-015 §5). ``append`` is episodic;
#: ``replace`` marks a living/current-state document. Defined here rather than
#: in ``core/parser.py`` for the same reason ``CATEGORIES`` and ``MEMORY_TYPES``
#: are: the surfaces that must agree on it should all import one tuple, and this
#: module is the one every surface can import (see the dependency note at the
#: top). ``core/parser.py`` re-exports it, so the many
#: ``from palinode.core.parser import VALID_UPDATE_POLICIES`` call sites are
#: unaffected.
VALID_UPDATE_POLICIES: tuple[str, ...] = ("append", "replace")

#: The canonical epistemic-marker enum (ADR-018) — the KIND of claim a memory
#: makes, orthogonal to ``type``. The ABSENCE of the field is its own state
#: (``unmarked``) and is deliberately NOT a member: for an audit-grade store,
#: "nobody declared this" must not silently inherit the authority of "verified".
VALID_EPISTEMICS: tuple[str, ...] = (
    "fact",
    "inference",
    "open_question",
    "unverified",
)

#: The Auditable Memory Records (AMR) specification version every record the
#: save path writes declares in its ``auditable_memory`` frontmatter field.
#: The declaration is REQUIRED by the spec (§4.1) and is what makes a
#: conforming record distinguishable from one that merely looks similar — an
#: implementation that emits no declaration conforms at no level (§7).
AMR_SPEC_VERSION: str = "0.1"

#: Versions the save surface recognises. A caller-supplied
#: ``auditable_memory`` outside this set is rejected, never guessed at (§4.1).
VALID_AMR_VERSIONS: tuple[str, ...] = (AMR_SPEC_VERSION,)

#: The canonical read-tier enum — how much of a memory a caller wants
#: back. Tiers are deterministic views computed at read time, never a second
#: content store; ``full`` is the content unchanged and is what every surface
#: returns when the caller says nothing.
TIERS: tuple[str, ...] = (
    "abstract",
    "overview",
    "full",
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
    ),
    # ── read ────────────────────────────────────────────────────────────────
    Operation(
        name="read",
        canonical_params=(
            CanonicalParam(name="file_path", type="string", required=True),
            CanonicalParam(name="meta", type="boolean"),
            CanonicalParam(name="tier", type="string", enum=TIERS),
        ),
        cli_command="read",
        mcp_tool="palinode_read",
        api_endpoint=("GET", "/read"),
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
            CanonicalParam(name="types", type="array", enum=MEMORY_TYPES),
            CanonicalParam(name="min_priority", type="integer"),
            CanonicalParam(name="date_after", type="string"),
            CanonicalParam(name="date_before", type="string"),
            CanonicalParam(name="include_daily", type="boolean"),
            # ADR-015 §5: telemetry-exclusion override; default false so
            # monitoring writes don't pollute recall. First-class on all surfaces.
            CanonicalParam(name="include_telemetry", type="boolean"),
            # How much of each hit to render. Omitted → unchanged
            # behaviour (snippet + content), so tiering is opt-in.
            CanonicalParam(name="tier", type="string", enum=TIERS),
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
                enum=VALID_UPDATE_POLICIES,
            ),
            # ADR-018: epistemic marker — the KIND of claim the memory makes
            # (fact / inference / open_question / unverified), orthogonal to
            # ``type``. It shipped first-class on all four surfaces while going
            # unregistered here, because the param test only walks this registry
            # outward: an unregistered param was not a case, so nothing could
            # fail. Registering it is what makes it enforceable.
            CanonicalParam(
                name="epistemic",
                type="string",
                enum=VALID_EPISTEMICS,
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
            # Which corpora to consolidate; defaults to daily/ on every surface.
            # Without it the deterministic executor was reachable only for
            # daily notes, never for typed memories.
            CanonicalParam(name="sources", type="array"),
        ),
        cli_command="consolidate",
        mcp_tool="palinode_consolidate",
        api_endpoint=("POST", "/consolidate"),
        known_drift={},
    ),
    # ── archive (on-demand ARCHIVE / SUPERSEDE for one named memory) ──
    # The addressable counterpart to the TTL sweep below: retires *this*
    # memory rather than everything whose expiry passed. `superseded_by` is
    # what makes it a SUPERSEDE — one verb, one optional argument, because
    # both end at `status: archived` and differ only in whether a successor
    # is named. Plugin-exempt like its maintenance/git-provenance siblings
    # (archive_expired, consolidate, rollback).
    Operation(
        name="archive",
        canonical_params=(
            CanonicalParam(name="file_path", type="string", required=True),
            CanonicalParam(name="reason", type="string"),
            CanonicalParam(name="superseded_by", type="string"),
        ),
        cli_command="archive",
        mcp_tool="palinode_archive",
        api_endpoint=("POST", "/archive"),
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
            "migrate bullets",
            "migrate frontmatter",
            "migrate openclaw",
            "obsidian-sync",
            # One-time local repair of rotted projects/*-status.md files: it
            # rewrites files on disk, exposes no memory-semantic operation, and
            # is deliberately CLI-only (dry-run by default, human commits).
            "repair-status",
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
INVENTORY_BACKLOG: dict[Surface, dict[str, int | InventoryBacklogEntry]] = {
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


def inventory_backlog_capabilities(surface: Surface) -> frozenset[str]:
    """Return canonical backlog identifiers plus their alternate names."""
    capabilities = set(INVENTORY_BACKLOG[surface])
    for entry in INVENTORY_BACKLOG[surface].values():
        if isinstance(entry, InventoryBacklogEntry):
            capabilities.update(entry.aliases)
    return frozenset(capabilities)


def by_name(op_name: str) -> Operation:
    """Look up an operation by name.  Raises ``KeyError`` if missing."""
    for op in REGISTRY:
        if op.name == op_name:
            return op
    raise KeyError(op_name)


def required_surfaces(op: Operation) -> frozenset[Surface]:
    """Return the surfaces this operation must appear on (i.e. not exempt).

    ``plugin`` is OPT-IN, not opt-out (ADR-019): a plugin is a delivery
    adapter over the REST API, and non-implementation of an operation is the
    expected case, not drift. An operation joins the plugin surface's parity
    obligations only by naming a ``plugin_tool``; nothing else about the
    registry puts it there. Before this flip, 13 operations each paid an
    ``exempt_surfaces={"plugin"}`` line for what was simply the default
    reality — and every new operation silently owed the plugin an
    implementation unless its author remembered the exemption.

    The first-party surfaces (cli / mcp / api) keep opt-out semantics:
    they ARE capability surfaces, and an operation missing from one of them
    is exactly the drift ADR-010 exists to catch.
    """
    first_party: frozenset[Surface] = frozenset({"cli", "mcp", "api"})
    surfaces = first_party - op.exempt_surfaces
    if op.plugin_tool is not None and "plugin" not in op.exempt_surfaces:
        surfaces |= {"plugin"}
    return surfaces


__all__ = [
    "ADMIN_EXEMPT_OPERATIONS",
    "CATEGORIES",
    "CanonicalParam",
    "INVENTORY_BACKLOG",
    "InventoryBacklogEntry",
    "INVENTORY_INFRA",
    "MEMORY_TYPES",
    "Operation",
    "PROMPT_TASKS",
    "ParamType",
    "REGISTRY",
    "Surface",
    "by_name",
    "inventory_backlog_capabilities",
    "registered_capabilities",
    "required_surfaces",
]
