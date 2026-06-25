"""In-memory, per-IP request gating and the request-size cap (#556).

Extracted from the former ``routers/_shared.py`` junk drawer. A self-contained
sliding-window limiter: counters keyed by ``ip:category`` reset each window,
expired entries are pruned inline, and the counter dict is capped so a stream of
unique client IPs can't inflate memory without bound.
"""

from __future__ import annotations

import os
import time
from typing import Any

# Rate limiting (in-memory, per-IP, resets each window).
# L2: prune expired entries inline so a stream of unique client IPs cannot
# inflate _rate_counters without bound. We also cap the dict at
# PALINODE_RATE_LIMIT_MAX_KEYS (default 10_000); when full the oldest
# window_start gets evicted so the limiter still serves real traffic.
_RATE_LIMIT_WINDOW = 60  # seconds
_RATE_LIMIT_SEARCH = int(os.environ.get("PALINODE_RATE_LIMIT_SEARCH", 100))
_RATE_LIMIT_WRITE = int(os.environ.get("PALINODE_RATE_LIMIT_WRITE", 30))
_RATE_LIMIT_MAX_KEYS = int(os.environ.get("PALINODE_RATE_LIMIT_MAX_KEYS", 10_000))
_rate_counters: dict[str, dict[str, Any]] = {}

_MAX_REQUEST_BYTES = int(os.environ.get("PALINODE_MAX_REQUEST_BYTES", 5 * 1024 * 1024))


def _prune_rate_counters(now: float) -> None:
    """Drop entries whose window has expired and cap at _RATE_LIMIT_MAX_KEYS.

    Cheap path when the dict is small: linear scan of expired keys (the
    limiter window is already short — 60s default — so the live set stays
    small in practice). Eviction is by oldest window_start, which approximates
    LRU well enough for a memory cap and avoids dragging in OrderedDict.
    """
    expired = [
        k
        for k, v in _rate_counters.items()
        if now - v["window_start"] > _RATE_LIMIT_WINDOW
    ]
    for k in expired:
        _rate_counters.pop(k, None)

    if len(_rate_counters) >= _RATE_LIMIT_MAX_KEYS:
        # Evict oldest 10% so we don't pay this cost on every call.
        evict_count = max(1, len(_rate_counters) - _RATE_LIMIT_MAX_KEYS + 1)
        oldest = sorted(
            _rate_counters.items(), key=lambda kv: kv[1]["window_start"]
        )[:evict_count]
        for k, _ in oldest:
            _rate_counters.pop(k, None)


def _check_rate_limit(client_ip: str, category: str, limit: int) -> bool:
    """Return True if request is within rate limit, False if exceeded."""
    now = time.time()
    _prune_rate_counters(now)
    key = f"{client_ip}:{category}"
    entry = _rate_counters.get(key)
    if not entry or now - entry["window_start"] > _RATE_LIMIT_WINDOW:
        _rate_counters[key] = {"window_start": now, "count": 1}
        return True
    entry["count"] += 1
    return entry["count"] <= limit
