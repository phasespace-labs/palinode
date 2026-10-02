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

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from palinode.core.config import config
from palinode.core.scope import ScopeChain, resolve_scope_chain, scope_level_name

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
    #: Working directory used for the shared, git-aware ADR-008 resolution.
    cwd: str | None = None
    #: Explicit project slug or entity ref; overrides cwd resolution.
    project: str | None = None
    #: How the caller resolved ``project``, when it resolved one itself. A
    #: stdio MCP server resolves its client's pinned ``PALINODE_PROJECT`` in
    #: its own process and forwards the result, because the API process does
    #: not have that client's environment; without this the digest would
    #: report a configured scope as an argument someone typed. Reporting only
    #: — it never changes which project is used, and anything outside the
    #: closed vocabulary is rejected rather than echoed.
    project_resolved_by: str | None = None
    #: Caller-generated session identifier; lands on the scope chain's
    #: session level.
    session_id: str | None = None
    #: ``classic`` = all ``core: true`` memories; ``scoped`` = additionally
    #: chain-filter explicit ``scope:`` frontmatter. Omitted → the configured
    #: ``scope.prime_mode``. ``smart`` is Layer 3 and rejected (422).
    mode: Literal["classic", "scoped"] | None = None
    #: Explicit chain override (ADR-009 §3.5).
    scope: ScopeOverride | None = None
    # Hook/session-start callers set this so automatic exclusions are enforced
    # before the digest reads memory.  The MCP tool remains explicit unless it
    # deliberately opts into the automatic classification.
    automatic: bool = False
    source_path: str | None = None


@router.post("/context/prime")
def context_prime_api(req: PrimeRequest) -> dict[str, Any]:
    """Session-start context digest for the resolved scope.

    Returns ``{project, core_memories, recent_decisions, open_action_items,
    recent_snapshots, _palinode_hint, mode, scope_chain, receipt}`` — the
    bounded ADR-012 digest (built
    from frontmatter reads only; no embeds, no LLM), plus the resolved scope
    chain. Project-scoped rows are returned only when a project actually
    resolves; with neither a usable ``cwd`` nor a ``project``, the digest
    degrades to core memories only and never guesses a scope.

    ``receipt`` is the delivery receipt for the digest
    (:mod:`palinode.core.receipt`, public view): every supplied ref at the
    exact revision it was supplied at, its disposition, the known origin
    lineage, the resolved scope and policy, the evaluation time and the next
    known temporal transition among the delivered records. No retrieval-log
    rows are written for a prime: a session-start injection ledger is a
    separate contract, and this endpoint has never written to that log.
    """
    from palinode.core.capture_policy import evaluate_capture_policy
    from palinode.core.context_prime import (
        RESOLUTION_BASES,
        ProjectResolution,
        build_context_digest,
        resolve_context,
    )
    from palinode.core.receipt import build_digest_receipt

    decision = evaluate_capture_policy(
        "recall",
        automatic=req.automatic,
        cwd=req.cwd,
        project=req.project or (req.scope.project if req.scope else None),
        source_path=req.source_path,
    )
    if not decision.allowed:
        raise HTTPException(status_code=403, detail=decision.reason)

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
    resolution = resolve_context(cwd=req.cwd, project=project_arg)
    if req.project_resolved_by and resolution.basis == "explicit":
        if req.project_resolved_by not in RESOLUTION_BASES:
            raise HTTPException(status_code=422, detail="Unknown project resolution source")
        resolution = ProjectResolution(resolution.project, req.project_resolved_by)
    resolved = resolution.project

    if req.scope is not None:
        levels = req.scope.model_dump()
        for level in ("session", "agent", "harness", "member", "org"):
            levels[level] = scope_level_name(level, levels[level])
        if levels["project"]:
            override_project = resolve_context(project=levels["project"]).project
            levels["project"] = override_project.split("/", 1)[1] if override_project else None
        chain = ScopeChain(**levels)
    else:
        bare = resolved.split("/", 1)[1] if resolved else None
        chain = resolve_scope_chain(config, project=bare, session_id=req.session_id)

    digest = build_context_digest(
        cwd=req.cwd,
        project=project_arg,
        scope_chain=chain if mode == "scoped" else None,
        resolution=resolution,
    )

    logger.info(
        "prime: mode=%s cwd=%s session=%s chain=%s -> %d core memories",
        mode,
        req.cwd,
        req.session_id,
        chain.as_list(),
        len(digest.get("core_memories", [])),
    )
    receipt = build_digest_receipt(
        digest,
        # The request as it decides selection — ``session_id`` is telemetry and
        # is excluded by the receipt's own normalization.
        request={"surface": "context_prime", "cwd": req.cwd,
                 "project": project_arg, "mode": mode},
        scope=chain.as_list(),
        memory_dir=config.memory_dir,
    )
    return {
        **digest,
        "mode": mode,
        "scope_chain": chain.as_list(),
        "receipt": receipt.public(),
    }
