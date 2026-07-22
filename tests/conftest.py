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

import copy
import os

import pytest


def pytest_configure(config: pytest.Config) -> None:
    """Pin the terminal environment so a local run matches CI.

    Several CLI tests assert plain substrings against ``rich``-rendered output.
    ``rich`` colourises when ``FORCE_COLOR``/``CLICOLOR_FORCE`` is present in the
    environment *regardless of its value* — so a developer who exports either one
    sees a handful of CLI tests fail on embedded ANSI escapes while CI (no TTY,
    no forcing var) stays green. Tests that fail only on your machine train you
    to ignore local failures, which is the same "green means nothing" problem as
    a flaky abort. No test asserts colour is present, so pinning it off is safe.
    """
    os.environ.pop("FORCE_COLOR", None)
    os.environ.pop("CLICOLOR_FORCE", None)
    os.environ.setdefault("NO_COLOR", "1")


def _snapshot_config(obj: object) -> dict:
    """Deep-snapshot a palinode config object as a nested plain dict."""
    snap: dict = {}
    for key, value in vars(obj).items():
        if type(value).__module__.startswith("palinode") and hasattr(value, "__dict__"):
            snap[key] = _snapshot_config(value)
        else:
            snap[key] = copy.deepcopy(value)
    return snap


def _restore_config(obj: object, snap: dict) -> None:
    """Restore a snapshot *in place*, preserving every nested object's identity."""
    for key, value in snap.items():
        current = getattr(obj, key, None)
        if isinstance(value, dict) and type(current).__module__.startswith("palinode"):
            _restore_config(current, value)
        else:
            setattr(obj, key, value)


@pytest.fixture(autouse=True)
def _isolate_global_config():
    """Restore the process-wide ``config`` singleton after every test.

    ``palinode.core.config.config`` is a module-level singleton that ~30 fixtures
    across the suite mutate (``config.memory_dir``, ``config.git.auto_commit``, …)
    and then restore with plain statements after a bare ``yield``. Those restores
    are skipped when the test fails, so **one failure silently reconfigures every
    later test** — pointing ``memory_dir`` at a deleted ``tmp_path`` or leaving
    ``git.auto_commit`` off. That turns a single red test into a cascade of
    unrelated red tests and makes outcomes depend on ordering.

    This runs before any test-module fixture and restores in a ``finally``, so
    the leak cannot escape a test regardless of how it ends. It is a safety net,
    not a licence: a fixture that mutates global state should still use
    ``monkeypatch`` or ``try/finally``.
    """
    from palinode.core.config import config

    snapshot = _snapshot_config(config)
    try:
        yield
    finally:
        _restore_config(config, snapshot)


@pytest.fixture(autouse=True)
def _warm_embed_gate(monkeypatch):
    from palinode.indexer import index_file as index_file_mod

    monkeypatch.setattr(index_file_mod, "_embeds_deferred", lambda client: False)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Make session teardown deterministic w.r.t. palinode's background writers.

    Tests that exercise ``POST /reindex`` or construct a ``PalinodeHandler``
    directly leave a 10 s debounce timer armed. Those daemon timers used to fire
    during pytest's capture teardown (``ValueError: I/O operation on closed
    file``) and, when one was mid-write as the interpreter finalized, took the
    whole run down with ``_enter_buffered_busy`` / SIGABRT — exit 134 *after*
    every test had passed (#677).

    Two steps, in order:

    1. Stop the timers. ``palinode.indexer.watcher`` registers the same call via
       ``atexit``; doing it here as well moves cleanup ahead of pytest's capture
       teardown instead of after it.
    2. Detach palinode's module-level *stream* handlers. They bound
       ``sys.stderr`` at import — i.e. pytest's capture replacement — so any
       straggler (an abandoned ``doctor`` check finishing late, a third-party
       thread) would write into a closed buffer. File handlers stay attached;
       they own a real file, not a captured stream.
    """
    import logging
    import sys

    watcher_mod = sys.modules.get("palinode.indexer.watcher")
    if watcher_mod is not None:
        watcher_mod.shutdown_handlers()

    names = [n for n in logging.root.manager.loggerDict if n.startswith("palinode")]
    for name in [*names, "palinode"]:
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            if isinstance(handler, logging.StreamHandler) and not isinstance(
                handler, logging.FileHandler
            ):
                logger.removeHandler(handler)
