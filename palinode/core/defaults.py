"""
Palinode cross-surface defaults — single source of truth.

Per ADR-010 (Cross-Surface Parity Contract), every parameter default that
appears on more than one surface lives here. Surfaces (CLI, MCP, REST API,
plugin) import from this module rather than baking literals like ``or 0.75``
into surface-specific code paths.

The ``test_surface_parity`` test asserts that the parity registry's
``default_key`` references all resolve here, and that no surface uses a
literal default that contradicts a value defined here.

If a default needs to be configurable, expose it via ``palinode.core.config``
and re-export here. The contract is "one place to look" — not "one place to
hard-code."
"""
from __future__ import annotations

from palinode.core.config import config


# ── Search ───────────────────────────────────────────────────────────────────

#: Default ``limit`` for ``search`` and ``list`` operations.
SEARCH_LIMIT_DEFAULT: int = 3

#: Default similarity ``threshold`` for ``search`` (server-side).  Driven by
#: ``config.search.api_threshold`` so deployments can tune via YAML.
SEARCH_THRESHOLD_DEFAULT: float = config.search.api_threshold

#: Default similarity threshold for ambient/MCP search (typically tighter than
#: explicit search to keep auto-context noise low).  Driven by config.
SEARCH_THRESHOLD_MCP: float = config.search.mcp_threshold


# ── Triggers ─────────────────────────────────────────────────────────────────

#: Lowest score any trigger fires at, whatever threshold it was given.
TRIGGER_FLOOR: float = 0.40

#: Threshold a trigger gets when it is registered without one. Chosen together
#: with TRIGGER_MIN_SHARED_TERMS on the dogfood store's triggers (2026-10-31,
#: bge-m3, the shipped check_triggers on a throwaway copy): with the gate,
#: 0.62 had the best F1 and F0.5 of 0.55-0.70 over 38 held-out on-target and
#: 78 off-target prompts (30/38 fired, 3/78 false fires), against 25/38 and
#: 8/78 for 0.65 without the gate. 0.58 fired 32/38 with 7/78 false fires;
#: 0.65 with the gate fired 24/38 with 1/78. Without the gate no threshold
#: worked: ordinary maintainer prompts reach 0.60-0.71.
TRIGGER_THRESHOLD_DEFAULT: float = 0.62

#: Required lead over the next eligible trigger, after max-over-phrasings scoring.
TRIGGER_LEAD_MARGIN: float = 0.06

#: Content words the prompt must share with the winning trigger's own text
#: (description + phrasings, porter-stemmed) before it fires. Whole-prompt
#: cosine cannot separate a trigger's intent from everyday prompts in the
#: same vocabulary ("reply in the thread" scored 0.709 against the
#: contributor-reply trigger). On the dogfood store at 0.62, 3 cut everyday
#: false fires from 13/58 to 1/58 for one on-target miss (31/38 -> 30/38);
#: 2 left 7/58 and 4 dropped on-target to 23/38. Store-wide term rarity did not
#: separate: the everyday words ("respond", "assign") are rarer there than the
#: trigger's own. On the synthetic trigger benchmark, whose terse prompts share
#: fewer words with their triggers, 3 costs recall (35/56 -> 21/56 at 0.62)
#: with no false-fire change (1/28), so phrasings should use the user's words.
TRIGGER_MIN_SHARED_TERMS: int = 3

#: Default cooldown (in hours) between consecutive firings of the same trigger.
TRIGGER_COOLDOWN_HOURS_DEFAULT: int = 24


# ── Diff / history ───────────────────────────────────────────────────────────

#: Default lookback window (in days) for ``diff``.
DIFF_DAYS_DEFAULT: int = 7

#: Default page size for ``history``.
HISTORY_LIMIT_DEFAULT: int = 20


# ── Save ─────────────────────────────────────────────────────────────────────

#: HTTP request header used to attribute writes to a source surface.
#:
#: Per ADR-010, surfaces SHOULD set this header on every API call rather than
#: putting ``source`` into the body.  The API uses the header value when
#: ``source`` is not in the body, which lets us keep a stable contract while
#: each surface maintains its own attribution.
SAVE_SOURCE_HEADER: str = "X-Palinode-Source"

#: Source attribution sentinel used when the header is absent and no body
#: ``source`` is supplied.  ``"api"`` matches the FastAPI server's prior
#: behavior, so this default does not change observable attribution.
SAVE_SOURCE_API_DEFAULT: str = "api"


# ── List ─────────────────────────────────────────────────────────────────────

#: HTTP response header on a project-scoped ``GET /list``: how many records
#: tagged to another project the listing left out. The body stays the bare
#: array every existing consumer parses, so the count travels beside it —
#: the job ``retrieval.other_projects_withheld`` does in a search receipt.
LIST_OTHER_PROJECTS_WITHHELD_HEADER: str = "X-Palinode-Other-Projects-Withheld"


# ── Session-end timeouts ──────────────────────────────────────────────

#: HTTP request timeout (in seconds) for ``POST /session-end`` on every
#: surface that calls it directly (CLI, MCP).  90 seconds matches the
#: embed-path budget introduced in 9d5ec74 (fix(mcp): raise embed-path
#: timeouts to 90 s; add GPU keepalive).  Multi-decision payloads + BGE-M3
#: dedup embed + git commit on a remote host can together exceed the old 30s
#: default and produce misleading "is palinode running?" errors.
#:
#: The env-var ``PALINODE_SESSION_END_TIMEOUT`` overrides this at runtime so
#: operators on faster infra don't have to rebuild to tighten it.
SESSION_END_TIMEOUT_SECONDS: float = float(
    __import__("os").environ.get("PALINODE_SESSION_END_TIMEOUT", "90")
)

#: Sentinel for cross-surface drift assertions.  Any module that imports
#: ``SESSION_END_TIMEOUT_SECONDS`` should assert it equals this at import
#: time, so a future maintainer who changes the default here will immediately
#: see which surfaces must be updated.
_SESSION_END_TIMEOUT_SENTINEL: float = 90.0

# ── Save timeout ──────────────────────────────────────────────────────

#: HTTP request timeout (in seconds) for ``POST /save`` on the CLI and MCP.  A
#: slow inline embed can outlast it after the write has landed; see
#: ``embeddings.defer_embed_on_save``.  ``PALINODE_SAVE_TIMEOUT`` overrides it.
def _save_timeout_seconds() -> float:
    # Read at import, so a malformed value must not raise: every entrypoint
    # imports this module. Same policy as the other env overrides in config.
    import logging
    import math
    import os

    raw = os.environ.get("PALINODE_SAVE_TIMEOUT")
    if raw is None:
        return 30.0
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value <= 0:
        logging.getLogger("palinode.config").warning(
            "ignoring malformed env override; keeping the default of 30 s "
            "var=PALINODE_SAVE_TIMEOUT value=%r",
            raw,
        )
        return 30.0
    return value


SAVE_TIMEOUT_SECONDS: float = _save_timeout_seconds()

# ── Session-end dedup ─────────────────────────────────────────────────

#: Lookback window (in minutes) over which ``session_end`` checks recently
#: indexed saves for semantic overlap.  60 minutes covers a typical Claude
#: Code session: long enough to catch /ps → /wrap reformulations of the same
#: decision.  Separate sessions are kept apart by identity, not by this
#: window: only saves whose frontmatter carries the entry's own
#: ``session_id`` are candidates, and an entry with no ``session_id`` is
#: never suppressed.
SESSION_END_DEDUP_WINDOW_MINUTES: int = 60

#: Cosine-similarity threshold above which a recent save is considered a
#: duplicate of the new session-end content.  0.85 is conservative — BGE-M3
#: routinely scores unrelated documents around 0.4-0.6 and near-paraphrases
#: 0.8-0.95.  Above 0.85 we have high confidence the prior save already
#: captures the same semantic information.
SESSION_END_DEDUP_THRESHOLD: float = 0.85


# ── Consolidation timeout ────────────────────────────────────────────────────

#: HTTP request timeout (in seconds) for ``POST /consolidate`` on every surface
#: that calls it over HTTP (CLI, MCP).
#:
#: The 30 s (CLI) / 30 s (MCP) client defaults are budgets for *deterministic*
#: routes.  Consolidation is not one: the server gives the model 600 s per
#: project group (``_call_llm_with_fallback``) and calls it once per group, so
#: any pass that reaches a model outlives a default-budget client.  Giving up
#: first cancels nothing — the API keeps running, keeps the store's run lock,
#: and the next invocation gets a 409 while the first run's result is visible
#: only in the server log.
#:
#: 900 s is the server's per-call budget plus margin for embed + git on a
#: multi-group pass.  It is a ceiling on *waiting*, not a fix: a store with
#: many project groups can still exceed it.  The durable fix is a job id the
#: caller polls; until that exists, ``PALINODE_CONSOLIDATE_TIMEOUT`` raises the
#: ceiling.  Surfaces read it through their own module attribute at call time,
#: never captured as a default argument, so an override is visible to the
#: message they print on timeout.
CONSOLIDATION_TIMEOUT_SECONDS: float = float(
    __import__("os").environ.get("PALINODE_CONSOLIDATE_TIMEOUT", "900")
)


# ── Path validation ──────────────────────────────────────────────────────────

#: Allowed memory-file extensions accepted by ``read``/``list`` and the like.
ALLOWED_MEMORY_EXTENSIONS: tuple[str, ...] = (".md",)


__all__ = [
    "SEARCH_LIMIT_DEFAULT",
    "SEARCH_THRESHOLD_DEFAULT",
    "SEARCH_THRESHOLD_MCP",
    "TRIGGER_FLOOR",
    "TRIGGER_THRESHOLD_DEFAULT",
    "TRIGGER_MIN_SHARED_TERMS",
    "TRIGGER_COOLDOWN_HOURS_DEFAULT",
    "DIFF_DAYS_DEFAULT",
    "HISTORY_LIMIT_DEFAULT",
    "SAVE_SOURCE_HEADER",
    "SAVE_SOURCE_API_DEFAULT",
    "LIST_OTHER_PROJECTS_WITHHELD_HEADER",
    "SESSION_END_TIMEOUT_SECONDS",
    "_SESSION_END_TIMEOUT_SENTINEL",
    "SESSION_END_DEDUP_WINDOW_MINUTES",
    "SESSION_END_DEDUP_THRESHOLD",
    "CONSOLIDATION_TIMEOUT_SECONDS",
    "ALLOWED_MEMORY_EXTENSIONS",
]
