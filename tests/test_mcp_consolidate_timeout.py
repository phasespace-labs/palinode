"""``palinode_consolidate`` must outwait the server's own LLM budget.

The MCP tool posted ``/consolidate`` with a hardcoded 300 s while the server
allows the *model* 600 s per project group. Every pass that reached a model
therefore outlived the tool call that asked for it — and nothing was
cancelled: the API finished the pass, held ``.palinode/consolidation.lock``
while it did, 409'd the next call, and the result existed only in the server
log. The same failure the CLI had, one surface over.

These tests drive the real dispatcher against a real FastAPI app (the actual
``/consolidate`` route) with the *runner* stubbed, never the DB, through a
transport that enforces the per-request read timeout the way a network
transport does — ``MockTransport`` and starlette's ``TestClient`` both *ignore*
timeouts, which is precisely why the existing suite could not see this bug.
Budgets are scaled down in-test so a 600 s-class assertion runs in under a
second.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import json
import threading
import time
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from palinode import mcp
from palinode.api.routers.consolidation import router as consolidation_router

#: The budget every deterministic MCP route still uses.
DISPATCH_DEFAULT_TIMEOUT = 30.0

#: ``_call_llm_with_fallback``'s per-model budget — the floor the tool must clear.
SERVER_LLM_BUDGET = 600.0

#: What the tool sent before the fix. Named so the regression is legible.
PRE_FIX_TIMEOUT = 300.0


@pytest.fixture()
def consolidate_app() -> TestClient:
    """The real ``/consolidate`` route, no server module and no lifespan."""
    app = FastAPI()
    app.include_router(consolidation_router)
    return TestClient(app, raise_server_exceptions=False)


def _timeout_enforcing_transport(
    tc: TestClient,
    recorded: list[float | None],
    *,
    enforce_at_most: float | None = None,
) -> httpx.MockTransport:
    """Dispatch to the in-process app, honouring the request's read timeout.

    The requested budget arrives in ``request.extensions["timeout"]`` — the
    value under test — and is recorded before dispatch. The app runs in a
    worker thread so a slow handler can be abandoned exactly as httpx abandons
    a slow socket: the request raises ``ReadTimeout`` and the handler keeps
    running, which is the production behaviour that made the old bare timeout
    message a lie.

    ``enforce_at_most`` shortens the enforced wait without changing what the
    client asked for, which is how the handling tests reach the timeout branch
    in milliseconds whatever the configured budget is.
    """
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)

    async def handler(request: httpx.Request) -> httpx.Response:
        read = request.extensions.get("timeout", {}).get("read")
        recorded.append(read)
        wait = read if enforce_at_most is None else min(read or enforce_at_most, enforce_at_most)
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(
            pool,
            lambda: tc.request(
                request.method,
                request.url.raw_path.decode("ascii"),
                content=request.content,
                headers=dict(request.headers),
            ),
        )
        try:
            served = await asyncio.wait_for(asyncio.shield(future), wait)
        except (asyncio.TimeoutError, TimeoutError):
            raise httpx.ReadTimeout("read timed out", request=request) from None
        return httpx.Response(
            status_code=served.status_code,
            headers=served.headers.multi_items(),
            content=served.content,
            request=request,
        )

    return httpx.MockTransport(handler)


@pytest.fixture()
def install_transport(monkeypatch: pytest.MonkeyPatch):
    """Point the MCP server's shared client at the app under test."""

    def _install(transport: httpx.MockTransport) -> None:
        monkeypatch.setattr(mcp, "_http_transport", transport)
        # The client is a process global; force a rebuild over the new
        # transport rather than reusing whatever a previous test left behind.
        monkeypatch.setattr(mcp, "_http_client", None)
        monkeypatch.setattr(mcp, "_http_client_loop", None)

    return _install


def _stub_runner(
    monkeypatch: pytest.MonkeyPatch,
    *,
    result: dict[str, Any] | None = None,
    sleep: float = 0.0,
    blocks: threading.Event | None = None,
) -> list[dict[str, Any]]:
    """Replace the consolidation runner — not the store — with a slow stub."""
    from palinode.consolidation import runner

    calls: list[dict[str, Any]] = []

    def fake_run_consolidation(
        lookback_days: int | None = None,
        dry_run: bool = False,
        llm_fn: Any = None,
        sources: Any = None,
    ) -> dict[str, Any]:
        calls.append({"dry_run": dry_run, "sources": sources})
        if sleep:
            time.sleep(sleep)
        if blocks is not None:
            # Released by the test as soon as it has its assertion; the 10 s
            # ceiling only keeps a failing test from hanging the suite.
            blocks.wait(timeout=10)
        return result or {"status": "success", "stats": {"projects": 0}}

    monkeypatch.setattr(runner, "run_consolidation", fake_run_consolidation)
    return calls


def _dispatch(name: str, arguments: dict[str, Any]) -> str:
    """One tool call, returning the text a host would see."""
    content = asyncio.run(mcp._dispatch_tool(name, arguments))
    return content[0].text


# ---------------------------------------------------------------------------
# The budget
# ---------------------------------------------------------------------------


def test_consolidate_asks_for_at_least_the_servers_llm_budget(
    monkeypatch: pytest.MonkeyPatch, consolidate_app: TestClient, install_transport
) -> None:
    recorded: list[float | None] = []
    calls = _stub_runner(monkeypatch, result={"status": "success", "stats": {"p": 1}})
    install_transport(_timeout_enforcing_transport(consolidate_app, recorded))

    text = _dispatch("palinode_consolidate", {"dry_run": True})

    assert json.loads(text)["status"] == "success"
    assert calls == [{"dry_run": True, "sources": None}]

    assert recorded == [pytest.approx(mcp._CONSOLIDATE_TIMEOUT)]
    assert recorded[0] >= SERVER_LLM_BUDGET
    assert recorded[0] != PRE_FIX_TIMEOUT
    # Per-request override, not a new default for every tool: the deterministic
    # routes keep the tight budget that makes a dead API obvious.
    assert inspect.signature(mcp._post).parameters["timeout"].default == (
        DISPATCH_DEFAULT_TIMEOUT
    )


def test_budget_is_the_cross_surface_constant() -> None:
    """One budget, one env var, both surfaces — not two 900s that can drift."""
    from palinode.cli import _api
    from palinode.core import defaults

    assert mcp._CONSOLIDATE_TIMEOUT == defaults.CONSOLIDATION_TIMEOUT_SECONDS
    assert _api.CONSOLIDATION_TIMEOUT_SECONDS == defaults.CONSOLIDATION_TIMEOUT_SECONDS
    assert defaults.CONSOLIDATION_TIMEOUT_SECONDS >= SERVER_LLM_BUDGET


def test_consolidate_outlives_a_pass_longer_than_the_deterministic_default(
    monkeypatch: pytest.MonkeyPatch, consolidate_app: TestClient, install_transport
) -> None:
    """Scale model of the reported failure: the pass takes longer than the
    budget a deterministic route would have allowed, and the tool still
    returns its result."""
    monkeypatch.setattr(mcp, "_CONSOLIDATE_TIMEOUT", 5.0)
    recorded: list[float | None] = []
    _stub_runner(monkeypatch, sleep=0.4, result={"status": "success", "stats": {}})
    install_transport(_timeout_enforcing_transport(consolidate_app, recorded))

    text = _dispatch("palinode_consolidate", {})

    assert json.loads(text)["status"] == "success"
    assert recorded == [5.0]


def test_archive_paths_clear_the_deterministic_default(install_transport) -> None:
    """The sibling write routes: neither calls a model, both outrun 30 s on a
    large store, and both must match the budget the CLI sends."""
    recorded: dict[str, float | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        recorded[request.url.path] = request.extensions.get("timeout", {}).get("read")
        return httpx.Response(
            200,
            json={"file": "projects/x.md", "history_file": "projects/x-history.md"},
            request=request,
        )

    install_transport(httpx.MockTransport(handler))

    _dispatch("palinode_archive", {"file_path": "projects/x.md"})
    _dispatch("palinode_archive_expired", {"dry_run": True})

    assert recorded["/archive"] == 120.0
    assert recorded["/archive-expired"] == 120.0
    assert min(recorded.values()) > DISPATCH_DEFAULT_TIMEOUT


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


def test_timeout_returns_a_structured_report_not_a_bare_error(
    monkeypatch: pytest.MonkeyPatch, consolidate_app: TestClient, install_transport
) -> None:
    recorded: list[float | None] = []
    release = threading.Event()
    _stub_runner(monkeypatch, blocks=release)
    install_transport(
        _timeout_enforcing_transport(consolidate_app, recorded, enforce_at_most=0.25)
    )

    try:
        text = _dispatch("palinode_consolidate", {})
    finally:
        release.set()

    payload = json.loads(text)
    assert payload["status"] == "timeout"
    assert payload["server_still_running"] is True
    assert payload["timeout_seconds"] == mcp._CONSOLIDATE_TIMEOUT
    assert payload["lock"].endswith("consolidation.lock")
    assert payload["log"] == "logs/consolidation.log"
    assert "409" in payload["message"]
    assert "PALINODE_CONSOLIDATE_TIMEOUT" in payload["message"]
    # Not the dispatcher's generic timeout text, which says only that the
    # request timed out and invites a retry straight into a 409.
    assert text != mcp._timeout_message("palinode_consolidate")
    assert not text.startswith(mcp.DISPATCH_ERROR_PREFIXES)


def test_timeout_report_is_the_same_on_both_surfaces() -> None:
    """An agent and an operator must be told the same thing about the same run."""
    from palinode.cli.consolidate import _timeout_report

    assert mcp._consolidation_timeout_report(900.0) == _timeout_report(900.0)


def test_timeout_report_names_the_run_locks_own_path() -> None:
    """The lock path is quoted from the lock, not retyped."""
    from palinode.consolidation.run_lock import LOCK_RELATIVE_PATH

    assert mcp._consolidation_timeout_report(900.0)["lock"] == str(LOCK_RELATIVE_PATH)
