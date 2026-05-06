"""Direct unit tests for palinode.api.rate_limit (#325).

These exercise the extracted rate-limiting module without FastAPI.
"""
from __future__ import annotations

import time

import pytest


@pytest.fixture(autouse=True)
def _clean_counters():
    """Isolate rate-limit state per test."""
    from palinode.api.rate_limit import _rate_counters

    saved = _rate_counters.copy()
    _rate_counters.clear()
    yield
    _rate_counters.clear()
    _rate_counters.update(saved)


def test_check_under_limit_passes():
    from palinode.api.rate_limit import check

    for _ in range(5):
        assert check("10.0.0.1", "search", limit=10)


def test_check_at_limit_blocks():
    from palinode.api.rate_limit import check

    for _ in range(3):
        assert check("10.0.0.2", "write", limit=3)
    assert not check("10.0.0.2", "write", limit=3)


def test_prune_counters_removes_old_entries():
    from palinode.api.rate_limit import _rate_counters, prune_counters, WINDOW

    stale_ts = time.time() - (WINDOW * 3)
    _rate_counters["old:search"] = {"window_start": stale_ts, "count": 5}
    _rate_counters["fresh:search"] = {"window_start": time.time(), "count": 1}

    prune_counters(time.time())
    assert "old:search" not in _rate_counters
    assert "fresh:search" in _rate_counters
