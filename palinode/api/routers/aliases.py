"""Operator routes for the store's curated entity aliases.

  GET  /aliases          groups, each ref with its file count
  GET  /aliases/check    the alias lint plus project_tags_unmapped
  POST /aliases/add      create or extend a group (dry_run previews)
  POST /aliases/remove   drop a member; an emptied group goes (dry_run previews)

The file is always ``<PALINODE_DIR>/entity-aliases.yaml``: no request field
names a path. There is deliberately no MCP twin for the writes — alias groups
decide what project-scoped recall shows, so changing them stays with the
operator (see ``palinode.core.parity``).
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, StrictBool

from palinode.core import alias_curation
from palinode.core.alias_curation import AliasCurationError
from palinode.core.path_guard import PathTraversalError

router = APIRouter()


class AliasAddRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    canonical: str
    members: list[str]
    move: StrictBool = False
    dry_run: StrictBool = False


class AliasRemoveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    member: str
    dry_run: StrictBool = False


def _fail(exc: AliasCurationError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


@router.get("/aliases")
def aliases_list_api() -> dict[str, Any]:
    try:
        return alias_curation.list_groups()
    except AliasCurationError as exc:
        raise _fail(exc) from None


@router.get("/aliases/check")
def aliases_check_api() -> dict[str, Any]:
    return alias_curation.check()


@router.post("/aliases/add")
def aliases_add_api(req: AliasAddRequest) -> dict[str, Any]:
    try:
        return alias_curation.add(
            req.canonical, req.members, move=req.move, dry_run=req.dry_run
        )
    except AliasCurationError as exc:
        raise _fail(exc) from None
    except PathTraversalError:
        raise HTTPException(status_code=400, detail="alias file resolves outside the store") from None


@router.post("/aliases/remove")
def aliases_remove_api(req: AliasRemoveRequest) -> dict[str, Any]:
    try:
        return alias_curation.remove(req.member, dry_run=req.dry_run)
    except AliasCurationError as exc:
        raise _fail(exc) from None
    except PathTraversalError:
        raise HTTPException(status_code=400, detail="alias file resolves outside the store") from None
