from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from palinode.api._util import _safe_500, effective_project_scope
from palinode.api.path_safety import _open_memory_file_text, _resolve_memory_path
from palinode.core import embedder, expiry, parser, store
from palinode.core.lifecycle import eligibility
from palinode.core.visibility import is_visible
from palinode.core.scope import ScopeChain

router = APIRouter()


class TriggerRequest(BaseModel):
    description: str
    memory_file: str
    trigger_id: str | None = None
    threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    phrasings: list[str] = Field(default_factory=list)
    cooldown_hours: int | None = 24
    #: ISO-8601; after this the trigger no longer fires (``None`` = never).
    expires_at: str | None = None
    #: Free text — who/what licensed this trigger to act. Display-only.
    authority: str | None = None


class CheckTriggersRequest(BaseModel):
    query: str
    cwd: str | None = None
    project: str | None = None
    session_id: str | None = None
    cooldown_bypass: bool | None = False


def _trigger_target_deliverable(memory_file: str, chain: ScopeChain | None = None) -> bool:
    """Require a safe, readable, *current* target with discovery permission.

    Two gates, both read from the target's live frontmatter:

    - visibility (:func:`palinode.core.visibility.is_visible`) — a ``private``
      or off-chain ``restricted`` memory is never pushed into automatic recall;
    - lifecycle (:func:`palinode.core.lifecycle.eligibility`) — a retired
      target (archived, superseded, deprecated, retracted, expired, or under
      ``archive/``) is not deliverable either. A trigger fire is an unprompted
      assertion that the target is worth acting on now; retiring a memory has
      to stop that, or archiving it silently leaves one automatic path still
      presenting it as current. The classifier is the shared one the digest
      and the consolidation runner already select through, so the three cannot
      disagree about what "retired" means.

    The trigger row itself is untouched: registry inspection still lists it,
    and ``restore``-ing the target makes it deliverable again with no
    re-registration. ``check_triggers`` applies this *before* recording a
    fire, so an undeliverable target burns neither cooldown nor ``fire_count``.
    """
    candidates = [memory_file]
    if not memory_file.endswith(".md"):
        candidates.append(f"{memory_file}.md")
    for candidate in candidates:
        try:
            _, resolved = _resolve_memory_path(candidate)
            content = _open_memory_file_text(resolved)
            metadata, _ = parser.parse_frontmatter(content)
        except FileNotFoundError:
            continue
        except (HTTPException, OSError, ValueError):
            return False
        if not is_visible(chain, resolved, metadata=metadata):
            return False
        return not eligibility(metadata, path=resolved).retired
    return False


@router.post("/triggers")
def create_trigger_api(req: TriggerRequest) -> dict[str, Any]:
    """Register a new prospective trigger."""
    import uuid
    if req.expires_at is not None and expiry.parse_expires_at(req.expires_at) is None:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid expires_at {req.expires_at!r}; expected an ISO-8601 timestamp",
        )
    try:
        trigger_id = req.trigger_id or str(uuid.uuid4())
        emb = embedder.embed(req.description)
        if not emb:
            raise ValueError("Failed to embed trigger description")

        phrasing_embeddings = [embedder.embed(text) for text in req.phrasings]
        if any(not vector for vector in phrasing_embeddings):
            raise ValueError("Failed to embed trigger phrasing")

        store.add_trigger(
            trigger_id=trigger_id,
            description=req.description,
            memory_file=req.memory_file,
            embedding=emb,
            threshold=req.threshold,
            phrasings=req.phrasings,
            phrasing_embeddings=phrasing_embeddings,
            cooldown_hours=req.cooldown_hours or 24,
            expires_at=req.expires_at,
            authority=req.authority,
        )
        return {"id": trigger_id, "status": "created"}
    except embedder.EmbeddingInputError:
        raise  # typed 422 via the app-level handler in server.py
    except embedder.EmbeddingUnavailable:
        raise  # typed 503 via the app-level handler in server.py
    except Exception as e:
        raise _safe_500(e, "Trigger creation failed")


@router.get("/triggers")
def list_triggers_api() -> list[dict[str, Any]]:
    """List all registered triggers."""
    return store.list_triggers()


@router.delete("/triggers/{trigger_id}")
def delete_trigger_api(trigger_id: str) -> dict[str, str]:
    """Remove a trigger."""
    store.delete_trigger(trigger_id)
    return {"status": "deleted"}


@router.post("/check-triggers")
def check_triggers_api(req: CheckTriggersRequest) -> list[dict[str, Any]]:
    """Check context against prospective triggers in the client's scope."""
    from palinode.api.routers.search import _resolve_scope_chain

    scope = effective_project_scope(None, cwd=req.cwd, project=req.project)
    chain = _resolve_scope_chain(context=scope.context, session_id=req.session_id)
    try:
        emb = embedder.embed(req.query)
        if not emb:
            return []
        # Matching is store-wide, but only a visible, non-retired target's
        # metadata reaches automatic recall — and the verdict is made *before*
        # the fire is recorded, so a trigger that delivers nothing keeps its
        # cooldown and its fire_count. Registry inspection remains a
        # maintenance view over every trigger.
        fires = store.check_triggers(
            query_embedding=emb,
            query=req.query,
            cooldown_bypass=req.cooldown_bypass or False,
            deliverable=lambda target: _trigger_target_deliverable(target, chain),
        )
        from palinode.core.config import config
        from palinode.core.retrieval_log import RetrievalLogger

        RetrievalLogger(config.memory_dir).record_trigger_fires(
            fires, session_id=req.session_id, cwd=req.cwd,
            project=scope.project.removeprefix("project/") if scope.project else None,
        )
        return fires
    except embedder.EmbeddingInputError:
        raise  # typed 422 via the app-level handler in server.py
    except embedder.EmbeddingUnavailable:
        raise  # typed 503 via the app-level handler in server.py
    except Exception as e:
        raise _safe_500(e, "Trigger check failed")
