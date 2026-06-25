"""Palinode local read-only provenance UI (Phase 0).

Server-rendered HTML (Jinja2) served from the existing FastAPI app under
``/ui``. No build step, no CDN — templates and static assets ship in-package
and are offline-first. Loopback-only: the router refuses to serve when the API
is bound to a non-loopback address (see ``router.py``).

The UI is a pure client of existing capabilities (status / lint / read /
git lineage). It introduces no business logic the store/API does not already
expose. The markdown-render + sanitize + provenance helpers in ``render.py``
and ``provenance.py`` are deliberately store-agnostic so ``weir`` can reuse
them later.
"""
