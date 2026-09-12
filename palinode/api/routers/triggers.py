from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from palinode.api._util import _safe_500
from palinode.core import embedder, expiry, store

router = APIRouter()


class TriggerRequest(BaseModel):
    description: str
    memory_file: str
    trigger_id: str | None = None
    threshold: float | None = 0.75
    cooldown_hours: int | None = 24
    #: ISO-8601; after this the trigger no longer fires (``None`` = never).
    expires_at: str | None = None
    #: Free text — who/what licensed this trigger to act. Display-only.
    authority: str | None = None


class CheckTriggersRequest(BaseModel):
    query: str
    cooldown_bypass: bool | None = False


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

        store.add_trigger(
            trigger_id=trigger_id,
            description=req.description,
            memory_file=req.memory_file,
            embedding=emb,
            threshold=req.threshold or 0.75,
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
    """Check context against prospective triggers."""
    try:
        emb = embedder.embed(req.query)
        if not emb:
            return []
        results = store.check_triggers(
            query_embedding=emb,
            cooldown_bypass=req.cooldown_bypass or False
        )
        return results
    except embedder.EmbeddingInputError:
        raise  # typed 422 via the app-level handler in server.py
    except embedder.EmbeddingUnavailable:
        raise  # typed 503 via the app-level handler in server.py
    except Exception as e:
        raise _safe_500(e, "Trigger check failed")
