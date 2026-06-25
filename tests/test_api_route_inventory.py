"""Golden test pinning the FastAPI route surface of ``palinode.api.server``.

This is the **pre-split gate** for the staged router-split work (#314,
jobs #510–#512). Before any route is moved into a sub-router, the live
route inventory — path plus sorted HTTP methods — is captured in
``tests/fixtures/api_route_inventory.json``. A refactor that merely
relocates handlers into routers must leave this surface byte-for-byte
identical; if it doesn't, this test fails loudly and the regenerated
fixture has to be reviewed deliberately.

The inventory is computed exactly as it was generated::

    sorted(
        [route.path, sorted(route.methods)]
        for route in app.routes
        if hasattr(route, "methods") and route.methods
    )

which includes FastAPI built-ins (``/docs``, ``/redoc``,
``/openapi.json``). Determinism — not curation — is the point.

Env isolation: importing ``palinode.api.server`` reads auth/config env at
module scope (see ``tests/test_api_bearer_auth.py``). With none of the
``PALINODE_API_*`` vars set the module loads in its default loopback,
no-token configuration, so a plain import is safe here.
"""
from __future__ import annotations

import json
from pathlib import Path

from palinode.api.server import app

_FIXTURE = Path(__file__).parent / "fixtures" / "api_route_inventory.json"


def _live_route_inventory() -> list[list[object]]:
    """Return the live route surface as ``[[path, [methods...]], ...]``."""
    return sorted(
        [route.path, sorted(route.methods)]
        for route in app.routes
        if hasattr(route, "methods") and route.methods
    )


def test_api_route_inventory_matches_fixture() -> None:
    """The live FastAPI route surface must equal the pinned golden fixture."""
    live = _live_route_inventory()
    # json round-trips tuples to lists; the fixture is list-of-lists already.
    live = json.loads(json.dumps(live))
    golden = json.loads(_FIXTURE.read_text(encoding="utf-8"))

    assert live == golden, (
        "API route surface changed — routes added/removed/moved. "
        "If intentional, regenerate the fixture deliberately."
    )
