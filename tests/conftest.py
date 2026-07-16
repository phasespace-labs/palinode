"""Shared test fixtures.

The cold-embed gate in ``palinode.indexer.index_file``
(``_embeds_deferred``) runs a real ``probe_embed`` against the configured
Ollama URL the first time a process indexes anything. Unit tests have no
Ollama, so without intervention every mocked-embedder test would silently
take the deferred (FTS-only) path and fail its vector assertions. Default
the suite to a proven-warm embed path; cold-path tests
(``test_cold_save_fast_return.py``) re-patch explicitly.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _warm_embed_gate(monkeypatch):
    from palinode.indexer import index_file as index_file_mod

    monkeypatch.setattr(index_file_mod, "_embeds_deferred", lambda client: False)
