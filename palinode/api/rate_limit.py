"""In-memory, per-IP rate limiting with automatic pruning.

Extracted from ``palinode.api.server`` (#325) to keep the server module
focused on route definitions. The module-level ``_rate_counters`` dict is
the single shared state object — importers that need to clear it for test
isolation should import it directly.
"""
from __future__ import annotations

import os
import time
from typing import Any

__all__ = ["prune_counters", "check"]

# ── Configuration (env-tunable) ────────────────────────────────────────────
WINDOW = 60  # seconds
LIMIT_SEARCH = int(os.environ.get("PALINODE_RATE_LIMIT_SEARCH", 100))
LIMIT_WRITE = int(os.environ.get("PALINODE_RATE_LIMIT_WRITE", 30))
MAX_KEYS = int(os.environ.get("PALINODE_RATE_LIMIT_MAX_KEYS", 10_000))

# Shared mutable state — tests that monkeypatch this must import the dict
# object itself (not a copy) so mutations propagate.
_rate_counters: dict[str, dict[str, Any]] = {}


def prune_counters(now: float) -> None:
    """Drop entries whose window has expired and cap at ``MAX_KEYS``.

    Cheap path when the dict is small: linear scan of expired keys (the
    limiter window is already short — 60 s default — so the live set stays
    small in practice). Eviction is by oldest ``window_start``, which
    approximates LRU well enough for a memory cap.
    """
    expired = [
        k
        for k, v in _rate_counters.items()
        if now - v["window_start"] > WINDOW
    ]
    for k in expired:
        _rate_counters.pop(k, None)

    if len(_rate_counters) >= MAX_KEYS:
        # Evict oldest 10 % so we don't pay this cost on every call.
        evict_count = max(1, len(_rate_counters) - MAX_KEYS + 1)
        oldest = sorted(
            _rate_counters.items(), key=lambda kv: kv[1]["window_start"]
        )[:evict_count]
        for k, _ in oldest:
            _rate_counters.pop(k, None)


def check(client_ip: str, category: str, limit: int) -> bool:
    """Return ``True`` if request is within rate limit, ``False`` if exceeded."""
    now = time.time()
    prune_counters(now)
    key = f"{client_ip}:{category}"
    entry = _rate_counters.get(key)
    if not entry or now - entry["window_start"] > WINDOW:
        _rate_counters[key] = {"window_start": now, "count": 1}
        return True
    entry["count"] += 1
    return entry["count"] <= limit
