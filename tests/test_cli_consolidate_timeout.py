"""``palinode consolidate`` must outwait the server's own LLM budget.

The CLI client's 30 s default applied to ``POST /consolidate``, while the
server allows the *model* 600 s per project group. Every pass that reached a
model therefore outlived the CLI that asked for it: click printed a bare
``Aborted!``, exit 1, no JSON — and nothing was cancelled. The API finished the
pass, held ``.palinode/consolidation.lock`` while it did, and a second
invocation got 409. The first run's result existed only in the server log.

These tests drive the real command against a real FastAPI app with the
*runner* stubbed (never the DB) through a transport that enforces the
per-request read timeout the way a network transport does — ``MockTransport``
and ``TestClient`` both ignore timeouts, which is precisely why this bug was
invisible to the existing suite. Budgets are scaled down in-test so a
600 s-class assertion runs in under a second.
"""

from __future__ import annotations

import concurrent.futures
import json
import threading
import time
from typing import Any

import httpx
import pytest
from click.testing import CliRunner
from fastapi import FastAPI
from fastapi.testclient import TestClient

from palinode.api.routers.consolidation import router as consolidation_router
from palinode.cli import _api
from palinode.cli import main as cli

#: The client-wide default every other route still uses.
CLIENT_DEFAULT_TIMEOUT = 30.0

#: ``_call_llm_with_fallback``'s per-model budget — the floor the CLI must clear.
SERVER_LLM_BUDGET = 600.0


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
    running, which is the production behaviour that made the old ``Aborted!``
    a lie.

    ``enforce_at_most`` shortens the enforced wait without changing what the
    client asked for, which is how the handling tests reach the timeout branch
    in milliseconds whatever the configured budget is.
    """
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)

    def handler(request: httpx.Request) -> httpx.Response:
        read = request.extensions.get("timeout", {}).get("read")
        recorded.append(read)
        wait = read if enforce_at_most is None else min(read or enforce_at_most, enforce_at_most)
        future = pool.submit(
            tc.request,
            request.method,
            request.url.raw_path.decode("ascii"),
            content=request.content,
            headers=dict(request.headers),
        )
        try:
            served = future.result(timeout=wait)
        except concurrent.futures.TimeoutError:
            raise httpx.ReadTimeout("read timed out", request=request) from None
        return httpx.Response(
            status_code=served.status_code,
            headers=served.headers.multi_items(),
            content=served.content,
            request=request,
        )

    return httpx.MockTransport(handler)


def _install_cli_client(
    monkeypatch: pytest.MonkeyPatch,
    tc: TestClient,
    recorded: list[float | None],
    *,
    client_default: float = CLIENT_DEFAULT_TIMEOUT,
    enforce_at_most: float | None = None,
) -> httpx.Client:
    """Point the CLI's shared api_client at the app under test."""
    api = _api.PalinodeAPI(
        transport=_timeout_enforcing_transport(
            tc, recorded, enforce_at_most=enforce_at_most
        )
    )
    api.client.timeout = httpx.Timeout(client_default)
    monkeypatch.setattr(_api.api_client, "client", api.client)
    return api.client


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


# ---------------------------------------------------------------------------
# The budget
# ---------------------------------------------------------------------------


def test_consolidate_asks_for_at_least_the_servers_llm_budget(
    monkeypatch: pytest.MonkeyPatch, consolidate_app: TestClient
) -> None:
    recorded: list[float | None] = []
    calls = _stub_runner(monkeypatch, result={"status": "success", "stats": {"p": 1}})
    client = _install_cli_client(monkeypatch, consolidate_app, recorded)

    result = CliRunner().invoke(cli, ["consolidate", "--dry-run", "--format", "json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["status"] == "success"
    assert calls == [{"dry_run": True, "sources": None}]

    assert recorded == [pytest.approx(_api.CONSOLIDATION_TIMEOUT_SECONDS)]
    assert recorded[0] >= SERVER_LLM_BUDGET
    # Per-request override, not a new client-wide default: the deterministic
    # routes keep the tight budget that makes a dead API obvious.
    assert client.timeout.read == CLIENT_DEFAULT_TIMEOUT


def test_consolidate_outlives_a_pass_longer_than_the_client_default(
    monkeypatch: pytest.MonkeyPatch, consolidate_app: TestClient
) -> None:
    """Scale model of the reported failure: the pass takes longer than the
    client default and the command still returns its result."""
    monkeypatch.setattr(_api, "CONSOLIDATION_TIMEOUT_SECONDS", 5.0)
    recorded: list[float | None] = []
    _stub_runner(monkeypatch, sleep=0.4, result={"status": "success", "stats": {}})
    _install_cli_client(monkeypatch, consolidate_app, recorded, client_default=0.15)

    result = CliRunner().invoke(cli, ["consolidate", "--format", "json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["status"] == "success"
    assert recorded == [5.0]


def test_store_sweep_and_archive_clear_the_default_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sibling routes the issue names: whole-store sweep and archive."""
    recorded: dict[str, float | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        recorded[request.url.path] = request.extensions.get("timeout", {}).get("read")
        return httpx.Response(200, json={}, request=request)

    api = _api.PalinodeAPI(transport=httpx.MockTransport(handler))
    api.bootstrap_ids()
    api.archive("projects/x.md")

    assert recorded["/bootstrap-fact-ids"] == _api.STORE_SWEEP_TIMEOUT_SECONDS
    assert _api.STORE_SWEEP_TIMEOUT_SECONDS >= SERVER_LLM_BUDGET
    assert recorded["/archive"] > CLIENT_DEFAULT_TIMEOUT


# ---------------------------------------------------------------------------
# The message
# ---------------------------------------------------------------------------


def test_timeout_says_the_server_is_still_running(
    monkeypatch: pytest.MonkeyPatch, consolidate_app: TestClient
) -> None:
    recorded: list[float | None] = []
    release = threading.Event()
    _stub_runner(monkeypatch, blocks=release)
    _install_cli_client(monkeypatch, consolidate_app, recorded, enforce_at_most=0.25)

    try:
        result = CliRunner().invoke(cli, ["consolidate", "--format", "text"])
    finally:
        release.set()

    assert result.exit_code == 1
    # Rich wraps to 80 columns off a terminal; compare on collapsed whitespace.
    text = " ".join(result.output.split())
    assert "still running on the server" in text
    assert ".palinode/consolidation.lock" in text
    assert "logs/consolidation.log" in text
    assert "PALINODE_CONSOLIDATE_TIMEOUT" in text
    assert "409" in text
    assert "Aborted!" not in result.output


def test_timeout_is_machine_readable_when_piped(
    monkeypatch: pytest.MonkeyPatch, consolidate_app: TestClient
) -> None:
    recorded: list[float | None] = []
    release = threading.Event()
    _stub_runner(monkeypatch, blocks=release)
    _install_cli_client(monkeypatch, consolidate_app, recorded, enforce_at_most=0.25)

    try:
        result = CliRunner().invoke(cli, ["consolidate", "--format", "json"])
    finally:
        release.set()

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "timeout"
    assert payload["server_still_running"] is True
    assert payload["timeout_seconds"] == _api.CONSOLIDATION_TIMEOUT_SECONDS
    assert payload["lock"].endswith("consolidation.lock")
    assert payload["log"] == "logs/consolidation.log"
    assert "still running" in payload["message"]
