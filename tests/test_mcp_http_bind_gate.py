"""
tests/test_mcp_http_bind_gate.py — the MCP HTTP transport's public-bind gate
keys on the resolved bind host and defaults to loopback.

Mirror of the API server's gate: ``palinode-mcp-http`` used to default to
``0.0.0.0`` and only refuse when ``PALINODE_MCP_BIND_INTENT=public`` was also
set. Now the default bind is ``127.0.0.1``, and a non-loopback bind (flag or
env) with no ``PALINODE_API_TOKEN`` refuses to start unless the one shared
opt-out ``PALINODE_API_ALLOW_UNAUTH=1`` is set. There is no MCP-specific
opt-out twin — one knob per deployment.

Every test drives the real ``main_http()`` entry point with ``uvicorn.run``
replaced at the seam; the env / flag matrix is the caller-produced input.
One test runs the entry point in a fresh interpreter so the refusal is
observed the way systemd would observe it (non-zero exit, message on
stderr).
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

_GATE_ENV_VARS = (
    "PALINODE_MCP_HTTP_HOST",
    "PALINODE_MCP_HTTP_PORT",
    "PALINODE_MCP_SSE_HOST",
    "PALINODE_MCP_SSE_PORT",
    "PALINODE_MCP_BIND_INTENT",
    "PALINODE_MCP_ALLOW_UNAUTH",
    "PALINODE_API_ALLOW_UNAUTH",
    "PALINODE_API_BIND_INTENT",
    "PALINODE_API_TOKEN",
    "PALINODE_API_TOKEN_FILE",
)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _GATE_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def _capture_uvicorn(monkeypatch: pytest.MonkeyPatch) -> dict:
    import uvicorn

    captured: dict = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: captured.update(kw))
    return captured


def _run(monkeypatch: pytest.MonkeyPatch, argv: list[str] | None = None, **env: str) -> dict:
    import palinode.mcp as mcp_mod

    for key, value in env.items():
        monkeypatch.setenv(key, value)
    captured = _capture_uvicorn(monkeypatch)
    mcp_mod.main_http(argv or [])
    return captured


# ---------------------------------------------------------------------------
# Default bind is loopback
# ---------------------------------------------------------------------------


def test_default_bind_is_loopback(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _run(monkeypatch)
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 6341


# ---------------------------------------------------------------------------
# Refuse: non-loopback + no token + no opt-out
# ---------------------------------------------------------------------------


def _assert_refusal(
    exc: SystemExit,
    *,
    stated: str = "PALINODE_MCP_HTTP_HOST=0.0.0.0",
    absent: tuple[str, ...] = (),
) -> None:
    """``stated`` is how the refusal must spell the offending bind.

    The gate names a knob so the operator knows what to change, so the knob
    it names has to be the one that actually resolved the host — flag,
    canonical env var, or deprecated alias. ``absent`` pins the
    other sources out of the message: naming a variable the operator never
    set is the one failure a remediation line cannot afford.
    """
    msg = str(exc)
    assert "REFUSING TO START" in msg
    assert stated in msg, msg
    for needle in absent:
        assert needle not in msg, f"{needle!r} must not appear: {msg}"
    assert "PALINODE_API_TOKEN" in msg
    assert "PALINODE_API_ALLOW_UNAUTH=1" in msg, "the shared opt-out must be named"
    assert "no token of its own" in msg, (
        "the refusal must say plainly that the MCP HTTP transport has no token of "
        "its own — PALINODE_API_TOKEN is what protects the API it proxies to"
    )
    assert "PALINODE_MCP_ALLOW_UNAUTH" not in msg, "no MCP twin of the opt-out knob"


def test_env_non_loopback_no_token_refuses(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    import palinode.mcp as mcp_mod

    monkeypatch.setenv("PALINODE_MCP_HTTP_HOST", "0.0.0.0")
    captured = _capture_uvicorn(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        mcp_mod.main_http([])
    _assert_refusal(exc.value, absent=("--host",))
    assert not captured, "uvicorn must not be reached"


def test_flag_non_loopback_no_token_refuses(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate keys on the *resolved* host — ``--host 0.0.0.0`` is exactly as
    refused as the env var; the flag path cannot sneak past it.

    It must also *attribute* the bind to the flag: an operator who never set
    ``PALINODE_MCP_HTTP_HOST`` cannot act on advice to change it.
    """
    import palinode.mcp as mcp_mod

    captured = _capture_uvicorn(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        mcp_mod.main_http(["--host", "0.0.0.0"])
    _assert_refusal(
        exc.value,
        stated="--host 0.0.0.0",
        absent=("PALINODE_MCP_HTTP_HOST",),
    )
    assert "--host 127.0.0.1" in str(exc.value), "remedy must be spelled as a flag"
    assert not captured


def test_legacy_sse_host_env_is_gated_too(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    import palinode.mcp as mcp_mod

    monkeypatch.setenv("PALINODE_MCP_SSE_HOST", "0.0.0.0")
    _capture_uvicorn(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        mcp_mod.main_http([])
    _assert_refusal(exc.value, stated="PALINODE_MCP_SSE_HOST=0.0.0.0")


def test_mcp_twin_opt_out_does_not_exist(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``PALINODE_MCP_ALLOW_UNAUTH`` is not a knob — one opt-out per deployment."""
    import palinode.mcp as mcp_mod

    monkeypatch.setenv("PALINODE_MCP_HTTP_HOST", "0.0.0.0")
    monkeypatch.setenv("PALINODE_MCP_ALLOW_UNAUTH", "1")
    _capture_uvicorn(monkeypatch)
    with pytest.raises(SystemExit):
        mcp_mod.main_http([])


def test_intent_public_still_requires_token_even_with_opt_out(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``PALINODE_MCP_BIND_INTENT=public`` keeps meaning "token required";
    the bind-gate opt-out does not lift the intent gate."""
    import palinode.mcp as mcp_mod

    monkeypatch.setenv("PALINODE_MCP_HTTP_HOST", "0.0.0.0")
    monkeypatch.setenv("PALINODE_API_ALLOW_UNAUTH", "1")
    monkeypatch.setenv("PALINODE_MCP_BIND_INTENT", "public")
    captured = _capture_uvicorn(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        mcp_mod.main_http([])
    assert "PALINODE_MCP_BIND_INTENT=public requires" in str(exc.value)
    assert not captured


# ---------------------------------------------------------------------------
# Start: opt-out / token / loopback
# ---------------------------------------------------------------------------


def test_opt_out_starts_and_warns_every_start(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("WARNING", logger="palinode.mcp"):
        captured = _run(
            monkeypatch,
            PALINODE_MCP_HTTP_HOST="0.0.0.0",
            PALINODE_API_ALLOW_UNAUTH="1",
        )
    assert captured["host"] == "0.0.0.0"
    msgs = [r.getMessage() for r in caplog.records]
    assert any(
        "0.0.0.0" in m
        and "accessible from any network" in m
        and "PALINODE_API_ALLOW_UNAUTH=1 set" in m
        for m in msgs
    ), msgs


def test_opt_out_warning_names_the_source_that_set_the_bind(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The token-less start warning carries the same remediation line as the
    refusal, so it misattributes the same way if left alone."""
    with caplog.at_level("WARNING", logger="palinode.mcp"):
        captured = _run(
            monkeypatch,
            ["--host", "0.0.0.0"],
            PALINODE_API_ALLOW_UNAUTH="1",
        )
    assert captured["host"] == "0.0.0.0"
    warnings = [
        r.getMessage() for r in caplog.records if "accessible from any network" in r.getMessage()
    ]
    assert warnings, [r.getMessage() for r in caplog.records]
    assert any("--host 127.0.0.1" in m for m in warnings), warnings
    assert not any("PALINODE_MCP_HTTP_HOST" in m for m in warnings), warnings


def test_token_starts_without_exposure_warning(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("WARNING", logger="palinode.mcp"):
        captured = _run(
            monkeypatch,
            PALINODE_MCP_HTTP_HOST="0.0.0.0",
            PALINODE_API_TOKEN="t-secret",
        )
    assert captured["host"] == "0.0.0.0"
    assert not any("accessible from any network" in r.getMessage() for r in caplog.records)


def test_loopback_flag_overrides_non_loopback_env_and_starts(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Flag > env for the gate as well as the bind: ``--host 127.0.0.1`` on
    top of ``PALINODE_MCP_HTTP_HOST=0.0.0.0`` resolves loopback and starts
    token-less."""
    captured = _run(monkeypatch, ["--host", "127.0.0.1"], PALINODE_MCP_HTTP_HOST="0.0.0.0")
    assert captured["host"] == "127.0.0.1"


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_loopback_hosts_start_token_less(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    captured = _run(monkeypatch, PALINODE_MCP_HTTP_HOST=host)
    assert captured["host"] == host


# ---------------------------------------------------------------------------
# Fresh interpreter — what systemd sees
# ---------------------------------------------------------------------------


def test_refusal_in_fresh_interpreter_exits_nonzero() -> None:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
        "PALINODE_MCP_HTTP_HOST": "0.0.0.0",
    }
    for k in _GATE_ENV_VARS:
        if k not in env:
            env.pop(k, None)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import uvicorn; uvicorn.run = lambda *a, **k: None\n"
            "from palinode.mcp import main_http; main_http([])",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert "REFUSING TO START" in combined
    assert "PALINODE_MCP_HTTP_HOST=0.0.0.0" in combined
