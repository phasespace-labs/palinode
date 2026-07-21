"""Session-start context endpoints (ADR-012 Layer 4 + ADR-009 Layer 1).

``POST /context/prime`` is the session-start priming surface. The Claude Code
SessionStart hook POSTs ``{cwd, session_id}`` on every session start (shipped
forward-compat before the endpoint existed) and discards the body, so that
request shape is a frozen contract: bare hook-shaped payloads — including an
empty ``{}`` — must always succeed.

The response is the ADR-012 context digest (the same body backing the
``palinode_session_init`` MCP tool and ``palinode prime`` CLI), extended with
the ADR-009 scope fields: ``mode`` and the resolved ``scope_chain``. In
``scoped`` mode the digest's memory selection additionally drops memories
whose **explicit** ``scope:`` frontmatter is off the session's chain —
unscoped memories always pass (ADR-009 §7: no scope = works as before).

Reconciles ADR-009 §3.5 (explicit ``mode``/``scope`` overrides in the
request) with the PHASE-G G2 hook contract: one endpoint, server-side scope
resolution, optional explicit overrides. G2 warm-start and ``/context/save``
stay out of scope here; ``smart`` mode and budgets are Layer 3.
"""
from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel

from palinode.core.config import config
from palinode.core.scope import ScopeChain, resolve_scope_chain

logger = logging.getLogger("palinode.api")

router = APIRouter()

_PRIME_MODES = ("classic", "scoped")


class ScopeOverride(BaseModel):
    """Explicit scope override (ADR-009 §3.5 request shape). All levels optional.

    When present it **replaces** server-side chain resolution entirely — the
    request's ``cwd``/``session_id`` contribute nothing to the chain.
    """
    session: str | None = None
    agent: str | None = None
    harness: str | None = None
    project: str | None = None
    member: str | None = None
    org: str | None = None


class PrimeRequest(BaseModel):
    #: Working directory used to resolve the project scope (basename →
    #: project entity, via the ADR-008 resolution the search boost uses).
    cwd: str | None = None
    #: Explicit project slug or entity ref; overrides cwd resolution.
    project: str | None = None
    #: Caller-generated session identifier; lands on the scope chain's
    #: session level.
    session_id: str | None = None
    #: ``classic`` = all ``core: true`` memories; ``scoped`` = additionally
    #: chain-filter explicit ``scope:`` frontmatter. Omitted → the configured
    #: ``scope.prime_mode``. ``smart`` is Layer 3 and rejected (422).
    mode: Literal["classic", "scoped"] | None = None
    #: Explicit chain override (ADR-009 §3.5).
    scope: ScopeOverride | None = None


@router.post("/context/prime")
def context_prime_api(req: PrimeRequest) -> dict[str, Any]:
    """Session-start context digest for the resolved scope.

    Returns ``{project, core_memories, recent_decisions, open_action_items,
    recent_snapshots, _palinode_hint, mode, scope_chain}`` — the bounded
    ADR-012 digest (built
    from frontmatter reads only; no embeds, no LLM), plus the resolved scope
    chain. Project-scoped rows are returned only when a project actually
    resolves; with neither a usable ``cwd`` nor a ``project``, the digest
    degrades to core memories only and never guesses a scope.
    """
    from palinode.core.context_prime import build_context_digest, resolve_project

    configured = config.scope.prime_mode
    if configured not in _PRIME_MODES:
        logger.warning(
            "prime: unknown scope.prime_mode %r — using 'scoped'", configured
        )
        configured = "scoped"
    mode = req.mode or configured

    # An explicit scope override replaces resolution entirely (§3.5); its
    # project level also drives the digest's project sections so the response
    # stays internally coherent.
    project_arg = req.project or (req.scope.project if req.scope else None)
    resolved = resolve_project(cwd=req.cwd, project=project_arg)

    if req.scope is not None:
        chain = ScopeChain(**req.scope.model_dump())
    else:
        bare = resolved.split("/", 1)[1] if resolved else None
        chain = resolve_scope_chain(config, project=bare, session_id=req.session_id)

    digest = build_context_digest(
        cwd=req.cwd,
        project=project_arg,
        scope_chain=chain if mode == "scoped" else None,
    )

    logger.info(
        "prime: mode=%s cwd=%s session=%s chain=%s -> %d core memories",
        mode,
        req.cwd,
        req.session_id,
        chain.as_list(),
        len(digest.get("core_memories", [])),
    )
    return {**digest, "mode": mode, "scope_chain": chain.as_list()}
