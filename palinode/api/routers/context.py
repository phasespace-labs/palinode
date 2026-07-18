"""Session-start context endpoints (ADR-012 Layer 4)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()


class PrimeRequest(BaseModel):
    #: Working directory used to resolve the project scope (basename →
    #: project entity, via the ADR-008 resolution the search boost uses).
    cwd: str | None = None
    #: Explicit project slug or entity ref; overrides cwd resolution.
    project: str | None = None
    #: Accepted for SessionStart-hook compatibility; reserved for future
    #: session-scoped warming (ADR-009 scope chain).
    session_id: str | None = None


@router.post("/context/prime")
def context_prime_api(req: PrimeRequest) -> dict[str, Any]:
    """Session-start context digest for the resolved scope.

    Request body: ``{cwd?, project?, session_id?}``. Returns
    ``{project, core_memories, recent_decisions, open_action_items,
    _palinode_hint}`` — a bounded digest built from frontmatter reads only
    (no embeds, no LLM), safe on the session cold-start path. Project-scoped
    rows are returned only when a project actually resolves; with neither a
    usable ``cwd`` nor a ``project``, the digest degrades to core memories
    only and never guesses a scope.

    This is the endpoint the Claude Code SessionStart hook POSTs on every
    session start (shipped forward-compat before the endpoint existed), and
    the backing for the ``palinode_session_init`` MCP tool and the
    ``palinode prime`` CLI command.
    """
    from palinode.core.context_prime import build_context_digest

    return build_context_digest(cwd=req.cwd, project=req.project)
