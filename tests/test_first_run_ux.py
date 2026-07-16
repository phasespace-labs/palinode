"""First-run / cold-Ollama UX (#611).

Covers the four surfaces that made a fresh install "look broken":
  - OllamaClient.probe_embed — a real functional embed probe (not just a ping)
  - /status `embed_functional` cache helper — honest, bounded, cached
  - the doctor runner's bounded per-check timeout + streaming + crash-safety
  - the one-time keyword-only-mode notice on first embed failure

Network is driven through httpx.MockTransport (real httpx exception types); no
mock of the SQLite layer.
"""
from __future__ import annotations

import random
import threading
import time

import httpx
import pytest

from palinode.core.ollama_client import OllamaClient, OllamaError, RetryPolicy
from palinode.diagnostics.types import CheckResult, DoctorContext
from palinode.core.config import config


def _make_client(handler, *, retries=0):
    transport = httpx.MockTransport(handler)
    return OllamaClient(
        retry_policy=RetryPolicy(retries=retries),
        http_client=httpx.Client(transport=transport),
        sleep=lambda s: None,
        rng=random.Random(0),
    )


# ── probe_embed ────────────────────────────────────────────────────────────

def test_probe_embed_true_when_model_returns_vector():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"embeddings": [[0.1, 0.2, 0.3]]})

    client = _make_client(handler)
    assert client.probe_embed(timeout=1.0) is True


def test_probe_embed_false_when_embed_times_out():
    # Ollama daemon is up (a ping would pass) but the embed call hangs — the
    # exact cold-model false-green this probe exists to catch.
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("cold model", request=request)

    client = _make_client(handler)
    assert client.probe_embed(timeout=0.5) is False


def test_probe_embed_distinguishes_from_ping():
    # A daemon that answers a GET (ping True) but errors the embed (probe False).
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"models": []})
        raise httpx.ConnectError("embed refused", request=request)

    client = _make_client(handler)
    assert client.ping() is True
    assert client.probe_embed(timeout=0.5) is False


# ── /status embed_functional cache ─────────────────────────────────────────

class _FakeClient:
    def __init__(self, verdict: bool):
        self.verdict = verdict
        self.calls = 0

    def probe_embed(self, *, timeout: float = 2.0) -> bool:
        self.calls += 1
        return self.verdict


@pytest.fixture(autouse=True)
def _reset_embed_cache():
    from palinode.api.routers import health
    health._embed_probe_cache["ts"] = 0.0
    health._embed_probe_cache["ok"] = None
    yield
    health._embed_probe_cache["ts"] = 0.0
    health._embed_probe_cache["ok"] = None


def test_embed_functional_cached_reflects_probe_and_caches():
    from palinode.api.routers import health
    fake = _FakeClient(verdict=True)
    assert health._embed_functional_cached(fake) is True
    # Second call within TTL must not re-probe.
    assert health._embed_functional_cached(fake) is True
    assert fake.calls == 1


def test_embed_functional_cached_refreshes_after_ttl():
    from palinode.api.routers import health
    fake = _FakeClient(verdict=False)
    assert health._embed_functional_cached(fake) is False
    assert fake.calls == 1
    # Age the cache past the TTL → re-probe.
    health._embed_probe_cache["ts"] -= (health._EMBED_PROBE_TTL_S + 1)
    assert health._embed_functional_cached(fake) is False
    assert fake.calls == 2


# ── doctor runner: bounded timeout, streaming, crash-safety ────────────────

def _ctx() -> DoctorContext:
    return DoctorContext(config=config)


def _fast_check(ctx):
    return CheckResult(name="fast", severity="info", passed=True, message="ok")


def _slow_check(ctx):
    time.sleep(5)  # deliberately exceeds the test budget
    return CheckResult(name="slow", severity="info", passed=True, message="done")


def _raising_check(ctx):
    raise RuntimeError("boom")


def test_run_with_timeout_returns_result_when_fast():
    from palinode.diagnostics.runner import _run_with_timeout
    r = _run_with_timeout(_fast_check, _ctx(), 5.0)
    assert r.passed and r.name == "fast"


def test_run_with_timeout_reports_timeout_and_returns_promptly():
    from palinode.diagnostics.runner import _run_with_timeout
    started = time.monotonic()
    r = _run_with_timeout(_slow_check, _ctx(), 0.3)
    elapsed = time.monotonic() - started
    assert elapsed < 2.0, "runner must not block for the full check duration"
    assert r.passed is False and r.severity == "warn"
    assert "timed out" in r.message.lower()


def test_safe_call_converts_exception_to_error_result():
    from palinode.diagnostics.runner import _safe_call
    r = _safe_call(_raising_check, _ctx())
    assert r.passed is False and r.severity == "error"
    assert "boom" in r.message


def test_run_all_streams_each_result_and_survives_a_bad_check(monkeypatch):
    from palinode.diagnostics import runner
    checks = [(_fast_check, ()), (_raising_check, ()), (_fast_check, ())]
    monkeypatch.setattr(runner, "all_checks", lambda: checks)

    streamed = []
    results = runner.run_all(_ctx(), on_result=streamed.append, timeout_s=2.0)

    assert len(results) == 3          # a raising check does not abort the run
    assert len(streamed) == 3          # every result streamed as it landed
    assert [r.severity for r in results] == ["info", "error", "info"]


# ── keyword-only-mode one-time notice ──────────────────────────────────────

def test_keyword_only_notice_logs_exactly_once(caplog):
    import palinode.core.embedder as embedder
    embedder._keyword_only_notice_done = False
    with caplog.at_level("WARNING"):
        embedder._notice_keyword_only_once()
        embedder._notice_keyword_only_once()
    hits = [r for r in caplog.records if "keyword-only mode" in r.getMessage()]
    assert len(hits) == 1
    assert "ollama pull bge-m3" in hits[0].getMessage()
