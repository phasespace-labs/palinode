"""``GET /explain/{bundle_id}`` — explain one delivery from its receipt rows.

A thin translation. The whole composition lives in
:mod:`palinode.core.explain`, which reads the retrieval-event log the delivery
already wrote and never touches a second store. Read-only: nothing here writes
a file, a commit, recall metadata, or a retrieval-log row of its own — an
explanation is not a delivery, and logging one would put a row in the ledger
that describes reading the ledger.

Two request-shaped decisions are worth naming:

``session_id`` does two jobs, and they are separate gates. It resolves the
caller's scope chain exactly as ``/resolve`` does, so a record the caller could
not have been *shown* is not named by explaining a delivery that contained it.
It is also the identity a remote caller may present to read back its **own**
delivery's query prose.

``view=diagnostics`` asks for the caller-identifying half of the delivery — the
query prose and the session id. Asking is not enough. A bundle id is not a
secret (``GET /trace/{file}`` lists the ids of every delivery a file was
supplied in), so holding one must not authorise reading someone else's words.
The view is served only when the API is bound where nothing off this machine
can reach it — the same predicate the local inspector hard-refuses on — or when
the caller presents the session that made the delivery. Otherwise the response
is the ordinary public view with both fields marked ``withheld_diagnostics_only``
and the reason attached. **Not a 403**: refusing with a status code would answer
a question about the bundle that the caller has not earned.

``view`` is deliberately **not** a canonical parity parameter — the same
reasoning that keeps ``apply`` off the MCP lint tool. The agent-facing MCP and
plugin tools take the public view and cannot ask for anything else.
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query

from palinode.api._util import _retrieval_logger, _safe_500
from palinode.api.routers.search import _resolve_scope_chain

# The inspector's own bind predicate, imported rather than re-derived: this
# route serves the same two fields the inspector page does, and a lookalike
# test that drifted from it would hand one surface's refusal to the other
# surface's caller.
from palinode.api.ui.router import bind_is_loopback
from palinode.core.config import config
from palinode.core.explain import (
    DEFAULT_MAX_RECORDS,
    MAX_RECORDS_CEILING,
    InvalidBundleId,
    QueryAccess,
    explain_delivery,
    validate_bundle_id,
)

router = APIRouter()


@router.get("/explain/{bundle_id}")
def explain_api(
    bundle_id: str,
    session_id: str | None = None,
    view: Literal["public", "diagnostics"] = "public",
    limit: int = Query(default=DEFAULT_MAX_RECORDS, ge=1, le=MAX_RECORDS_CEILING),
) -> dict[str, Any]:
    """Explain what one delivery supplied, and what about it is not knowable.

    Returns the structured explanation: the supplied records at the exact
    revisions they were supplied at (and whether each source has changed
    since), the server-resolved scope, the recorded selection surface and
    demand, per-record dispositions, the delivery's coverage qualifiers and
    evaluation time — with every field the log never recorded rendered as an
    explicit ``unavailable`` marker naming why.

    A delivery that searched and supplied nothing is explained as exactly that.
    A reference with no rows behind it returns 200 with ``status:
    "not_found"`` and the candidate causes, because "the store has no record of
    this" is an answer about the store, not a missing resource.

    ``view="diagnostics"`` additionally asks for the query prose and session
    id, and is honoured only on a loopback bind or for the session that made
    the delivery; otherwise those two fields come back withheld with the
    reason, and the rest of the response is unchanged.
    """
    try:
        safe_id = validate_bundle_id(bundle_id)
    except InvalidBundleId as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    try:
        return explain_delivery(
            safe_id,
            memory_dir=config.memory_dir,
            chain=_resolve_scope_chain(session_id=session_id),
            query_access=QueryAccess(
                requested=view == "diagnostics",
                loopback=bind_is_loopback(),
                session_id=session_id,
            ),
            max_records=limit,
            log_enabled=_retrieval_logger.enabled,
        )
    except Exception as e:
        raise _safe_500(e, "Explain failed")
