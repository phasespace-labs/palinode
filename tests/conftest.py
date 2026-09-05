"""Shared test fixtures.

The cold-embed gate in ``palinode.indexer.reconcile``
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
import pathlib

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
    and often restore themselves after ``yield``. Pytest runs that teardown after
    a test-body failure, but cannot register it when fixture setup mutates config
    and then raises before reaching ``yield``. Without this earlier safety net,
    that narrower failure can point later tests at a deleted ``tmp_path`` or
    leave ``git.auto_commit`` off.

    This runs before any test-module fixture and restores in a ``finally``, so
    the setup-failure leak cannot escape a test. It is a safety net, not a
    licence: a fixture that mutates global state should still use ``monkeypatch``
    or register a finalizer before its first mutation.
    """
    from palinode.core.config import config

    snapshot = _snapshot_config(config)
    try:
        yield
    finally:
        _restore_config(config, snapshot)


@pytest.fixture(autouse=True)
def _isolate_doctor_search_roots(
    request: pytest.FixtureRequest,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    _isolate_global_config,
) -> None:
    """Keep doctor filesystem discovery inside each test's temp directory.

    An empty ``config.doctor.search_roots`` enables the production fallback,
    which includes the user's entire home directory. Tests that invoke doctor
    through the process-wide config must never inherit that machine-dependent
    behavior: it is slow, can discover a developer's real databases, and makes
    wall-clock results depend on the size of ``$HOME``.

    A test that specifically covers the production fallback may opt out with
    ``@pytest.mark.doctor_real_search_roots``. Keep that marker narrow: ordinary
    doctor behavior tests should provide explicit roots instead.
    """
    if request.node.get_closest_marker("doctor_real_search_roots") is not None:
        return

    from palinode.core.config import config

    monkeypatch.setattr(config.doctor, "search_roots", [str(tmp_path)])


@pytest.fixture(autouse=True)
def _warm_embed_gate(monkeypatch):
    from palinode.indexer import reconcile as reconcile_mod

    monkeypatch.setattr(reconcile_mod, "_embeds_deferred", lambda client: False)


@pytest.fixture(autouse=True)
def _bridge_scalar_embed_test_doubles(monkeypatch):
    """Keep existing scalar embed test doubles valid after batching.

    Write-path tests historically replace the public ``embed()`` seam to
    model success, outage, and partial failure. Production reconciliation now
    calls ``embed_many()``; when a test replaces ``embed()``, route the batch
    dynamically through that replacement so the test still exercises its
    intended condition instead of contacting a developer's Ollama instance.
    Tests that patch ``embed_many()`` directly override this bridge normally.
    """
    from palinode.core import embedder

    original_embed = embedder.embed
    original_embed_many = embedder.embed_many

    def embed_many(texts):
        if embedder.embed is not original_embed:
            return [embedder.embed(text) for text in texts]
        return original_embed_many(texts)

    monkeypatch.setattr(embedder, "embed_many", embed_many)


@pytest.fixture(autouse=True)
def _isolated_rate_counters(request: pytest.FixtureRequest) -> dict:
    """Clear the process-wide limiter while arming restoration first."""
    from palinode.api import rate_limit

    counters = rate_limit._rate_counters
    saved = counters.copy()

    def restore() -> None:
        counters.clear()
        counters.update(saved)

    request.addfinalizer(restore)
    counters.clear()
    return counters


@pytest.fixture(autouse=True)
def _isolate_db_path(tmp_path, monkeypatch):
    """Point ``config.db_path`` at this test's ``tmp_path`` by default.

    ``db_path`` is resolved to an **absolute** path when config loads, so a
    fixture that patches ``config.memory_dir`` does *not* move it — the store
    still opens at the process default. That default is the developer's real
    database, and tests were both writing to it and silently depending on its
    existence and schema.

    Redirecting per-test kills the whole class rather than patching offenders
    one at a time: three separate files had it, in two different shapes, and
    which ones were visible depended on whether the default file already
    existed on that machine. A test that wants a specific ``db_path`` still
    overrides this, because its own patch runs afterwards.

    ``tmp_path`` is the same directory most fixtures already use for
    ``memory_dir``, so the database lands inside the memory dir exactly as it
    would in production.
    """
    from palinode.core.config import config

    monkeypatch.setattr(config, "db_path", str(tmp_path / ".palinode.db"))


@pytest.fixture(scope="session")
def _default_db_path() -> pathlib.Path | None:
    """The db path the process resolves to before any test patches it."""
    from palinode.core.config import config

    return pathlib.Path(config.db_path) if config.db_path else None


_TESTS_ROOT = pathlib.Path(__file__).resolve().parent

# Suite directories under ``tests/`` where the process default database is the
# subject of the test, not an accident — exempt from the guard below.
#
# ``tests/live/`` drives a *running* server against the memory dir the harness
# just exported as ``PALINODE_DIR``, so ``config.db_path`` resolves to exactly
# the database those tests are supposed to fill. Every live save therefore
# trips a guard whose premise — "touching the resolved default means you forgot
# to redirect" — only holds for suites that run against ``tmp_path``. There is
# nothing to redirect to here: the real directory *is* the fixture.
#
# Named here rather than shadowed from a ``tests/live/conftest.py`` so the
# exemption sits one screen from the guard it narrows. It should read as
# deliberate, because it is, and it must stay exactly this wide: any other
# suite that finds the guard inconvenient is the offender the guard is for.
_DEFAULT_DB_GUARD_EXEMPT_DIRS = ("live",)


def _exempt_from_default_db_guard(item: pytest.Item) -> bool:
    """True when *item* lives under an exempt suite directory."""
    try:
        relative = item.path.resolve().relative_to(_TESTS_ROOT)
    except (AttributeError, ValueError):
        return False
    return bool(relative.parts) and relative.parts[0] in _DEFAULT_DB_GUARD_EXEMPT_DIRS


@pytest.fixture(autouse=True)
def _no_writes_to_the_default_db(request, _default_db_path):
    """Fail the test that opens a store at the *process default* db path.

    ``config.db_path`` is resolved to an **absolute** path when config loads.
    Patching ``config.memory_dir`` afterwards therefore does NOT move it — the
    store still opens wherever the default pointed. A fixture that patches only
    ``memory_dir`` and then invokes anything that opens a store is writing
    somewhere real.

    Where that lands is environment-dependent, which is what made this hard to
    see: on the dev rig it produced a stray ``.palinode.db`` beside the code and
    looked like a cosmetic litter problem, while on a developer machine the same
    code writes into **their actual memory database**. The symptom was reported;
    the mechanism is the same either way.

    Guarding the resolved default rather than the repo root catches both. The
    check is mtime-based because the file usually already exists — absence is
    not the signal, mutation is.

    Scoped away from ``_DEFAULT_DB_GUARD_EXEMPT_DIRS``, where writing the
    resolved default is the whole point of the suite. ``tests/`` and
    ``tests/integration/`` stay guarded; see that constant for why.
    """
    if _default_db_path is None or _exempt_from_default_db_guard(request.node):
        yield
        return

    def _stamp() -> tuple[bool, float]:
        try:
            return True, _default_db_path.stat().st_mtime_ns
        except FileNotFoundError:
            return False, 0.0

    before = _stamp()
    yield
    after = _stamp()
    if after != before:
        pytest.fail(
            f"{request.node.nodeid} wrote to {_default_db_path} — the process "
            "default database. It opened a store without redirecting "
            "`config.db_path`, which is absolute and unaffected by patching "
            "`config.memory_dir`. Patch both:\n"
            "    monkeypatch.setattr(config, 'memory_dir', str(tmp_path))\n"
            "    monkeypatch.setattr(config, 'db_path', str(tmp_path / '.palinode.db'))"
        )


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Make session teardown deterministic w.r.t. palinode's background writers.

    Tests that exercise ``POST /reindex`` or construct a ``PalinodeHandler``
    directly leave a 10 s debounce timer armed. Those daemon timers used to fire
    during pytest's capture teardown (``ValueError: I/O operation on closed
    file``) and, when one was mid-write as the interpreter finalized, took the
    whole run down with ``_enter_buffered_busy`` / SIGABRT — exit 134 *after*
    every test had passed.

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
