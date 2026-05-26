"""End-to-end stdio JSON-RPC test for every MCP tool (issue #344, Phase 2 of #342).

Spawns palinode-api on a random port, then drives palinode-mcp via real MCP
JSON-RPC over stdio. Catches transport-layer failures (stdio framing, JSON-RPC
ID handling, lifecycle, env-var inheritance, signal handling) that the
in-process test (#343) cannot.

The smoke-args registry is shared with #343 via
tests/integration/_smoke_args.py — both surfaces stay in lockstep.

Marked @pytest.mark.slow because subprocess spin-up costs ~3-5 seconds.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from tests.integration._smoke_args import TOOL_SMOKE_ARGS


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_health(port: int, proc: subprocess.Popen, timeout_s: float = 30.0) -> None:
    """Block until /health returns 200, or raise with subprocess output."""
    deadline = time.monotonic() + timeout_s
    last_err: str | None = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            out, err = proc.communicate(timeout=2)
            raise RuntimeError(
                f"palinode-api exited early (code={proc.returncode})\n"
                f"stdout: {out.decode(errors='replace')[-2000:]}\n"
                f"stderr: {err.decode(errors='replace')[-2000:]}"
            )
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/health", timeout=1.0)
            if r.status_code == 200:
                return
            last_err = f"HTTP {r.status_code}"
        except (httpx.ConnectError, httpx.TimeoutException, httpx.ReadError) as e:
            last_err = type(e).__name__
        time.sleep(0.3)
    raise RuntimeError(f"palinode-api never reached /health on :{port}: {last_err}")


@pytest.fixture(scope="module")
def api_subprocess(tmp_path_factory):
    """Boot palinode-api on a random port pointed at a tmp memory dir.

    Module-scoped to amortize ~3–5s startup. State is shared across the
    parametrized tool calls — Phase 2 verifies stdio transport, not tool
    semantics, so cross-test contamination is fine.
    """
    tmp_dir: Path = tmp_path_factory.mktemp("phase2-stdio")
    port = _free_port()

    for d in ("people", "projects", "decisions", "insights", "research", "inbox", "daily"):
        (tmp_dir / d).mkdir(parents=True, exist_ok=True)

    env = {
        **os.environ,
        "PALINODE_DIR": str(tmp_dir),
        "PALINODE_API_HOST": "127.0.0.1",
        "PALINODE_API_PORT": str(port),
    }
    # Clear any inherited auth/bind-intent settings from the dev machine —
    # keep the test sealed off from the developer's running palinode.
    env.pop("PALINODE_API_TOKEN", None)
    env.pop("PALINODE_API_TOKEN_FILE", None)
    env.pop("PALINODE_API_BIND_INTENT", None)

    # Use `python -m palinode.api.server` rather than the `palinode-api`
    # console script — the script may not be on PATH when pytest is invoked
    # without venv activation, but `sys.executable` always resolves to the
    # interpreter that imported `palinode`, so the module is guaranteed
    # importable.
    proc = subprocess.Popen(
        [sys.executable, "-m", "palinode.api.server"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for_health(port, proc)
    except Exception:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        raise

    # Pre-seed the smoke-target file so read/blame/history/cluster_neighbors
    # have a target to point at.
    httpx.post(
        f"http://127.0.0.1:{port}/save",
        json={
            "content": "Phase 2 stdio smoke target.",
            "type": "Insight",
            "slug": "smoke-target",
        },
        timeout=10.0,
    )

    yield port, str(tmp_dir), env

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _stdio_params(env: dict) -> StdioServerParameters:
    # Same rationale as the API subprocess: invoke via `python -m` so we don't
    # depend on the console script being on PATH.
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "palinode.mcp"],
        env=env,
    )


@pytest.mark.slow
@pytest.mark.asyncio
async def test_initialize_and_list_tools(api_subprocess):
    """Real stdio handshake: initialize succeeds and tools/list returns the
    full registered set."""
    _port, _tmp_dir, env = api_subprocess
    async with stdio_client(_stdio_params(env)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()
            names = {t.name for t in result.tools}

    expected = set(TOOL_SMOKE_ARGS.keys())
    missing = expected - names
    assert not missing, f"tools/list missing tools: {sorted(missing)}"


@pytest.mark.slow
@pytest.mark.asyncio
async def test_every_tool_dispatches_via_stdio(api_subprocess):
    """Every tool dispatches over real stdio JSON-RPC without protocol error.

    `isError` content is allowed — many tools legitimately surface error text
    in a sealed test env (no Ollama, no git remote, no network). The point of
    Phase 2 is to verify stdio JSON-RPC framing, lifecycle, ID handling, and
    env-var inheritance — tool semantics are covered by Phase 1 (#343).

    Runs all 25 tool calls in a single session so any transport regression
    surfaces as a clear stdio failure, not test-fixture noise.
    """
    _port, _tmp_dir, env = api_subprocess
    failures: list[str] = []

    async with stdio_client(_stdio_params(env)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            for tool_name, (args, _lenient) in TOOL_SMOKE_ARGS.items():
                try:
                    result = await session.call_tool(tool_name, args)
                except Exception as e:
                    failures.append(f"{tool_name}: protocol error: {type(e).__name__}: {e}")
                    continue

                if result is None:
                    failures.append(f"{tool_name}: no CallToolResult")
                    continue
                if not result.content:
                    failures.append(f"{tool_name}: empty content list")
                    continue
                for item in result.content:
                    if not hasattr(item, "type"):
                        failures.append(f"{tool_name}: content item missing type field")
                        break

    if failures:
        pytest.fail(
            "Stdio JSON-RPC tool dispatch failures:\n  "
            + "\n  ".join(failures)
        )
