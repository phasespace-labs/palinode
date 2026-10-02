"""``POST /resolve`` — the bounded-resolution operation over HTTP.

One request, one qualified bundle: what stands, what replaced what, what is
still contested, and what the store will not answer for. The whole decision
lives in :mod:`palinode.core.bundle`, so this router is a thin translation of
the canonical parameters (ADR-010) into a :class:`~palinode.core.bundle.
BundleRequest` and back out again. It writes no memory file, link, commit
or recall metadata. After rendering, its public receipt is
persisted as audit metadata, separately from retrieval events.

``session_id`` is accepted beyond the canonical parameter set for the same
reason ``/context/prime`` accepts it — harness hooks send it on every call —
and it is used only to resolve the requester's scope chain.

``cwd`` and ``project`` are the requester's project scope, accepted beyond the
canonical set on the same terms: they only resolve the project level of the
scope chain, through the shared resolver ``/context/prime`` and
``/controls/check`` already use. They exist because this server may be on a
different machine from its caller. The server's own working directory is never
consulted, so a request that sends neither is unscoped (or scoped by the
operator's pinned ``PALINODE_PROJECT``), never scoped to whatever checkout the
server process happens to run from.
"""
from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from palinode.api._util import _safe_500
from palinode.api.rate_limit import _RATE_LIMIT_SEARCH, _check_rate_limit
from palinode.api.routers.search import _resolve_scope_chain
from palinode.core.bundle import (
    DEFAULT_MAX_CHARS,
    DEFAULT_MAX_ITEMS,
    BundleBudget,
    BundleRequest,
    build_bundle,
)
from palinode.core.parity import RESOLVE_INTENTS

logger = logging.getLogger("palinode.api")
router = APIRouter()


class ResolveRequest(BaseModel):
    """One bounded-resolution request.

    ``query`` and ``ref`` are the two ways in — a natural-language question or
    an exact memory ref (a fact id: the path without ``.md``). At least one is
    required. ``context`` are refs the caller already holds: each becomes a
    seed of its own, so context a caller is carrying is *checked* rather than
    assumed current. ``intent`` is reserved at ``current_state``; as-of /
    known-at questions are deliberately not offered yet.
    """

    query: str | None = None
    ref: str | None = None
    context: list[str] | None = None
    intent: Literal[*RESOLVE_INTENTS] | None = None
    # Output budget. Whole units are dropped in priority order when it bites;
    # a conflict group is never split, only ever dropped and reported.
    max_items: int | None = Field(default=None, ge=0, le=50)
    max_chars: int | None = Field(default=None, ge=0, le=20000)
    # History on request. Off by default: an automatic caller (the per-turn
    # hook) gets what search would give it — no retired record.
    include_retired: bool = False
    session_id: str | None = None
    #: The requester's working directory, resolved through the shared ADR-008
    #: resolver exactly as ``/context/prime`` resolves it.
    cwd: str | None = None
    #: The requester's explicit project (slug or entity ref); beats ``cwd``.
    project: str | None = None
    # A request scoped to a project leaves out records tagged to a different
    # project; true delivers them too, each labelled with its own project.
    include_other_projects: bool = False


@router.post("/resolve")
def resolve_api(req: ResolveRequest, request: Request = None) -> dict[str, Any]:
    """Resolve a question (or an exact record) against current memory.

    Returns the bundle: ``selected`` / ``replaced`` / ``conflicts`` /
    ``insufficient``, with ``coverage``, ``source_revisions``, the delivery
    ``receipt`` (and its ``receipt_ref``) and the rendered ``text`` every
    surface shares. A receipt-only audit entry makes this delivery explainable
    without recording resolution analysis as another retrieval event.
    """
    if request:
        client_ip = request.client.host if request.client else "unknown"
        if not _check_rate_limit(client_ip, "search", _RATE_LIMIT_SEARCH):
            raise HTTPException(status_code=429, detail="Rate limit exceeded")
    try:
        bundle_request = BundleRequest(
            query=req.query,
            ref=req.ref,
            context=tuple(req.context or ()),
            intent=req.intent or "current_state",
            budget=BundleBudget(
                max_items=DEFAULT_MAX_ITEMS if req.max_items is None else req.max_items,
                max_chars=DEFAULT_MAX_CHARS if req.max_chars is None else req.max_chars,
            ),
            include_other_projects=req.include_other_projects,
            include_retired=req.include_retired,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    # The requester's project, resolved outside the 500 guard so an unusable
    # project value is a 400 from the shared handler, never a server error.
    # `context` is memory refs, not the ambient entity refs `/search` boosts
    # on, so it is never a source of project scope.
    from palinode.core.context_prime import resolve_context

    scope = resolve_context(cwd=req.cwd, project=req.project)
    try:
        # ADR-009 Layer 2: the requester's project and session identity.
        chain = _resolve_scope_chain(context=scope.context, session_id=req.session_id)
        bundle = build_bundle(bundle_request, chain=chain)
        body = bundle.to_dict()
        from palinode.core.config import config
        from palinode.core.retrieval_log import RetrievalLogger

        RetrievalLogger(
            config.memory_dir, enabled=config.instrumentation.capture_retrievals,
        ).record_bundle_receipt(
            bundle, bundle_request, cwd=req.cwd,
            project=scope.project.removeprefix("project/") if scope.project else None,
        )
        return body
    except Exception as e:
        raise _safe_500(e, "Resolve failed")
